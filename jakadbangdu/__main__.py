import argparse
import json
import logging
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from .broker import DhanData, DhanExecutor, PaperExecutor
from .config import DEFAULT_WATCHLIST, Settings
from .engine import Engine
from .journal import Journal

IST = ZoneInfo("Asia/Kolkata")


def build(s: Settings) -> Engine:
    data = DhanData(s)
    ex = DhanExecutor(data) if s.live else PaperExecutor()
    return Engine(s, data, ex, Journal(s.db_path))


def market_open(now=None) -> bool:
    now = now or datetime.now(IST)
    return now.weekday() < 5 and dtime(9, 15) <= now.time() <= dtime(15, 30)


def run_loop(e: Engine, symbols):
    """Monitor SL/target every minute; scan at 09:45 and 13:30; discretionary review at 12:00 and 15:00."""
    done = set()
    while True:
        now = datetime.now(IST)
        if market_open(now):
            for line in e.monitor():
                print(line)
            for hhmm, job in (("09:45", "scan"), ("13:30", "scan"), ("12:00", "review"), ("15:00", "review"), ("15:45", "audit")):
                key = (now.date(), hhmm)
                if now.strftime("%H:%M") >= hhmm and key not in done:
                    done.add(key)
                    if job == "audit":
                        if now.weekday() == 4 and e.j.closed_count():  # weekly self-audit on Fridays
                            e.review_strategy()
                    elif job == "scan":
                        print("SCAN:", json.dumps(e.scan(symbols)))
                    else:
                        for line in e.monitor(review=True):
                            print(line)
        time.sleep(e.s.monitor_interval_s)


def main():
    ap = argparse.ArgumentParser(prog="jakadbangdu")
    ap.add_argument("cmd", choices=["scan", "monitor", "run", "book", "trades", "export", "playbook", "report", "review"])
    ap.add_argument("--symbols", help="comma-separated NSE symbols (default: built-in watchlist)")
    ap.add_argument("--review", action="store_true", help="monitor: also let Jakadbangdu review open positions")
    ap.add_argument("--out", default="data/trades.csv")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    s = Settings()
    symbols = a.symbols.split(",") if a.symbols else DEFAULT_WATCHLIST

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

    if s.live:
        print("*** LIVE MODE: real Dhan orders will be placed ***")
    e = build(s)
    if a.cmd == "scan":
        print(json.dumps(e.scan(symbols), indent=1))
    elif a.cmd == "review":
        print(json.dumps(e.review_strategy().model_dump(), indent=1))
    elif a.cmd == "monitor":
        print("\n".join(e.monitor(review=a.review)) or "nothing to do")
    else:
        run_loop(e, symbols)


if __name__ == "__main__":
    main()
