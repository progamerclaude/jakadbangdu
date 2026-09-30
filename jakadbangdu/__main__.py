import argparse
import json
import logging
import logging.handlers
import os
import sys
import traceback
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from .broker import DhanData, DhanExecutor, PaperExecutor
from .config import DEFAULT_WATCHLIST, Settings
from .engine import Engine
from .journal import Journal

IST = ZoneInfo("Asia/Kolkata")


LIVE_CONFIRM = "YES_I_ACCEPT_REAL_MONEY_RISK"


def guard_live(s: Settings, journal: Journal) -> None:
    """Real money needs two deliberate acts: an explicit confirmation AND a paper track record."""
    if not s.live:
        return
    need = int(os.getenv("JAKAD_MIN_PAPER_TRADES", "20"))
    have = journal.closed_count("paper")
    if os.getenv("JAKAD_CONFIRM_LIVE") != LIVE_CONFIRM:
        sys.exit(f"Refusing LIVE mode: set JAKAD_CONFIRM_LIVE={LIVE_CONFIRM} to confirm real orders on Dhan.")
    if have < need:
        sys.exit(f"Refusing LIVE mode: only {have} closed paper trades, need {need} "
                 f"(override with JAKAD_MIN_PAPER_TRADES=<n> if you accept the risk).")


def preflight(s: Settings) -> bool:
    """Check every dependency before leaving the bot unattended. Places no orders."""
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            print(f"[ OK ] {name}: {fn()}")
        except Exception as e:
            ok = False
            print(f"[FAIL] {name}: {e}")

    print(f"mode = {s.mode.upper()}  (real orders: {'YES' if s.live else 'no, simulated fills'})")
    if rules_only():
        print("[ -- ] RULES-ONLY mode: no Anthropic key needed, no AI agents")
    else:
        check("ANTHROPIC_API_KEY set", lambda: "yes" if os.getenv("ANTHROPIC_API_KEY") else (_ for _ in ()).throw(RuntimeError("missing")))
        check("Anthropic API reachable", lambda: __import__("jakadbangdu.llm", fromlist=["x"]).client().messages.create(
            model=s.analyst_model, max_tokens=16, messages=[{"role": "user", "content": "ok"}]).stop_reason)

    def dhan_profile():
        from dhanhq import DhanLogin
        p = DhanLogin(s.dhan_client_id).user_profile(s.dhan_access_token)
        if str(p.get("dataPlan", "")).lower() not in ("active", "true"):
            raise RuntimeError(f"Data API plan not active: dataPlan={p.get('dataPlan')} validity={p.get('dataValidity')}")
        return f"token valid till {p.get('tokenValidity')}, dataPlan={p.get('dataPlan')}"
    check("Dhan profile + data plan", dhan_profile)
    d = None

    def data():
        nonlocal d
        d = DhanData(s)
        return f"NIFTY 50 last close {d.daily(d.NIFTY, 30)['close'].iloc[-1]:.0f}"
    check("Dhan market data", data)
    if d:
        check("Dhan quote API", lambda: f"RELIANCE ltp {d.ltp(d.resolve('RELIANCE'))}")
    check("Journal writable", lambda: str(Journal(s.db_path).path))
    print("READY" if ok else "NOT READY: fix the failures above")
    return ok


def rules_only() -> bool:
    return os.getenv("JAKAD_RULES_ONLY", "").lower() in ("1", "true", "yes")


def build(s: Settings) -> Engine:
    data = DhanData(s)
    ex = DhanExecutor(data) if s.live else PaperExecutor()
    if rules_only():   # no LLM, no API key: the setup traded mechanically
        from .rules_strategy import NullMarket, RulesTrader
        return Engine(s, data, ex, Journal(s.db_path), jakad=RulesTrader(), analysts=[], market_researcher=NullMarket())
    return Engine(s, data, ex, Journal(s.db_path))


def market_open(now=None) -> bool:
    now = now or datetime.now(IST)
    return now.weekday() < 5 and dtime(9, 15) <= now.time() <= dtime(15, 30)


SCHEDULE = (("09:45", "scan"), ("09:50", "manage"), ("12:00", "review"), ("13:30", "scan"),
            ("15:00", "review"), ("15:20", "manage"), ("15:45", "audit"))
GRACE_MIN = 60   # a job missed by more than this (bot was down) is skipped, not run late


def due_jobs(now: datetime, done: set) -> list[str]:
    """Jobs whose time has come today and that were not run/skipped yet. Trading days only."""
    if now.weekday() >= 5:
        return []
    out = []
    for hhmm, job in SCHEDULE:
        key = (now.date(), hhmm)
        if key in done:
            continue
        h, m = map(int, hhmm.split(":"))
        late = (now.hour * 60 + now.minute) - (h * 60 + m)
        if late >= 0:
            done.add(key)
            if late <= GRACE_MIN:
                out.append(job)
    return out


def run_job(e: Engine, job: str, symbols, now: datetime):
    if job == "scan":
        print(now, "SCAN:", json.dumps(e.scan(symbols)), flush=True)
    elif job == "manage":
        for line in e.manage_structure():
            print(now, line, flush=True)
    elif job == "review":
        for line in e.monitor(review=True):
            print(now, line, flush=True)
    elif job == "audit" and now.weekday() == 4 and e.j.closed_count():   # weekly self-audit, Fridays after close
        e.review_strategy()
        print(now, "strategy audit done", flush=True)


def run_loop(e: Engine, symbols):
    """Monitor SL every minute in market hours; scheduled scan / manage / review / audit jobs. Survives errors."""
    done: set = set()
    while True:
        now = datetime.now(IST)
        try:
            if market_open(now):
                for line in e.monitor():
                    print(now, line, flush=True)
            for job in due_jobs(now, done):
                if job == "audit" or market_open(now):
                    try:
                        run_job(e, job, symbols, now)
                    except Exception:   # one failed job must not kill the day
                        logging.error("job %s failed:\n%s", job, traceback.format_exc())
        except Exception:
            logging.error("loop error:\n%s", traceback.format_exc())
        time.sleep(e.s.monitor_interval_s)


def run_backtest(s: Settings, symbols, years: int):
    """Fetch (and cache) Dhan daily history, then run the rule-only backtest + walk-forward + robustness."""
    import pandas as pd
    from . import backtest as bt
    cache = s.db_path.parent / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    dd = None

    def load(sym, inst=None):
        nonlocal dd
        f = cache / f"{sym.replace(' ', '_').replace('&', 'and')}.csv"
        if f.exists() and (pd.Timestamp.now() - pd.Timestamp(f.stat().st_mtime, unit="s")).days < 1:
            return pd.read_csv(f, index_col=0, parse_dates=True)
        dd = dd or DhanData(s)
        df = dd.daily(inst or dd.resolve(sym), days=365 * years + 30)
        df.to_csv(f)
        return df

    dd = DhanData(s)
    nifty = load("NIFTY 50", dd.NIFTY)
    data = {}
    for sym in symbols:
        try:
            data[sym] = load(sym)
        except Exception as e:
            print("skip", sym, e)
    print(f"{len(data)} symbols, {nifty.index[0].date()} -> {nifty.index[-1].date()}")
    print(json.dumps(bt.walk_forward(data, nifty, s), indent=1))
    print("robustness (full period):")
    for r in bt.robustness(data, nifty, s):
        print(r)


def main():
    ap = argparse.ArgumentParser(prog="jakadbangdu")
    ap.add_argument("cmd", choices=["scan", "monitor", "run", "book", "trades", "export", "playbook", "report", "review", "manage", "backtest", "preflight"])
    ap.add_argument("--symbols", help="comma-separated NSE symbols (default: built-in watchlist)")
    ap.add_argument("--review", action="store_true", help="monitor: also let Jakadbangdu review open positions")
    ap.add_argument("--rules-only", action="store_true", help="no LLM/API key: trade the setup mechanically")
    ap.add_argument("--years", type=int, default=6, help="backtest history length")
    ap.add_argument("--out", default="data/trades.csv")
    a = ap.parse_args()
    if a.rules_only:
        os.environ["JAKAD_RULES_ONLY"] = "1"
    s = Settings()
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[
        logging.StreamHandler(), logging.handlers.RotatingFileHandler(s.db_path.parent / "jakad.log", maxBytes=5_000_000, backupCount=5)])
    symbols = a.symbols.split(",") if a.symbols else DEFAULT_WATCHLIST

    if a.cmd == "preflight":
        sys.exit(0 if preflight(s) else 1)
    if a.cmd == "backtest":
        return run_backtest(s, symbols, a.years)
    if a.cmd == "report":
        from .performance import Performance
        print(json.dumps(Performance(s, Journal(s.db_path)).report(), indent=1))
        return
    if a.cmd in ("book", "trades", "export", "playbook"):  # journal-only, no broker needed
        from .risk import Book
        from .strategy import load
        j = Journal(s.db_path)
        if a.cmd == "book":
            print(json.dumps(Book(s, j, load(s)).summary(), indent=1))
        elif a.cmd == "trades":
            for t in j.closed_trades(50)[::-1] + j.open_trades():
                print(dict(t) | {"analyst_reports": "...", "decision": "...", "review": bool(t["review"])})
        elif a.cmd == "export":
            print("wrote", j.export_csv(a.out))
        else:
            print(s.playbook_path.read_text() if s.playbook_path.exists() else "(no playbook yet)")
        return

    guard_live(s, Journal(s.db_path))
    if s.live:
        print("*** LIVE MODE: real Dhan orders will be placed ***")
    e = build(s)
    if a.cmd == "scan":
        print(json.dumps(e.scan(symbols), indent=1))
    elif a.cmd == "manage":
        print("\n".join(e.manage_structure()) or "nothing to trail or add")
    elif a.cmd == "review":
        print(json.dumps(e.review_strategy().model_dump(), indent=1))
    elif a.cmd == "monitor":
        print("\n".join(e.monitor(review=a.review)) or "nothing to do")
    else:
        run_loop(e, symbols)


if __name__ == "__main__":
    main()
