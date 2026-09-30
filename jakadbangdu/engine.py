"""Orchestration: scan -> analyst panel (parallel) -> Jakadbangdu -> risk gate -> execute -> monitor -> learn."""
import json
import logging
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pandas as pd

from . import indicators as ind
from . import setups
from .agents.analysts import MarketResearcher, stock_analysts
from .broker import DhanData, Instrument
from .config import Settings
from .jakadbangdu import Jakadbangdu
from .journal import Journal
from . import strategy as strat
from .performance import Performance
from .risk import Book

log = logging.getLogger("jakad")
CONSOLIDATE_EVERY = 5  # closed trades between playbook rewrites


class Engine:
    def __init__(self, s: Settings, data, executor, journal: Journal, jakad: Jakadbangdu | None = None,
                 analysts=None, market_researcher=None):
        self.s, self.data, self.exec, self.j = s, data, executor, journal
        self.jakad = jakad or Jakadbangdu(s.jakad_model)
        self.analysts = analysts or stock_analysts(s.analyst_model)
        self.market_researcher = market_researcher or MarketResearcher(s.analyst_model)
        self.strategy = strat.load(s)
        self.book = Book(s, journal, self.strategy)
        self.perf = self.book.perf

    # ---- playbook memory --------------------------------------------------
    def playbook(self) -> str:
        p = self.s.playbook_path
        return p.read_text() if p.exists() else ""

    # ---- screening: the owner's setup (deterministic), then RS ranking -----
    def shortlist(self, symbols: list[str], nifty: pd.DataFrame):
        held = {t["symbol"] for t in self.j.open_trades()}
        found = []
        for sym in symbols:
            if sym in held:
                continue
            try:
                inst = self.data.resolve(sym)
                d = setups.completed(self.data.daily(inst))
                sig = setups.pullback_signal(d, self.s, sym, nifty)
                if sig:
                    found.append((sig.rs60, inst, d, sig))
            except Exception as e:  # one bad symbol must not stop the scan
                log.warning("screen %s failed: %s", sym, e)
        found.sort(key=lambda x: -x[0])   # strongest relative strength first
        return [(i, d, sig) for _, i, d, sig in found[: self.s.shortlist_size]]

    def market_context(self) -> dict:
        out = {}
        for inst in (self.data.NIFTY, self.data.BANKNIFTY):
            d = self.data.daily(inst, 250)
            sn = ind.snapshot(d)
            out[inst.symbol] = {k: sn[k] for k in ("close", "ema21", "ema50", "ema200", "rsi14", "adx14", "atr_pct")}
            out[inst.symbol]["chg_1d_pct"] = round(100 * (d["close"].iloc[-1] / d["close"].iloc[-2] - 1), 2)
            out[inst.symbol]["chg_5d_pct"] = round(100 * (d["close"].iloc[-1] / d["close"].iloc[-6] - 1), 2)
        return out

    # ---- one scan cycle ---------------------------------------------------
    def scan(self, symbols: list[str]) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        if self.book.open_count >= self.s.max_open_positions:
            return {"action": "SKIP", "why": "max open positions reached"}
        if self.book.free - self.book.capital * self.s.min_free_capital_pct <= 0:
            return {"action": "SKIP", "why": "free-capital floor reached"}

        nifty = setups.completed(self.data.daily(self.data.NIFTY, 300))
        if self.s.require_market_uptrend and not setups.market_uptrend(nifty):
            return {"action": "SKIP", "why": "regime filter: NIFTY below its EMA50, no new entries"}
        picks = self.shortlist(symbols, nifty)
        if not picks:
            return {"action": "SKIP", "why": "no stock is in an EMA20>40>89>100 stack pulling back to the 20/40"}
        signals = {i.symbol: sig for i, _, sig in picks}
        quotes = self.data.quotes([p[0] for p in picks])
        mkt_data = self.market_context()

        def build(i: Instrument, d):
            q = quotes.get(i.symbol)
            if not q:
                return None
            intr = None
            if self.analysts:                      # intraday candles only feed the analysts
                try:
                    intr = self.data.intraday(i, 15, 5)
                except Exception:
                    pass
            return {"symbol": i.symbol, "today": today, "daily": d, "intraday": intr, "nifty": nifty, "quote": q}

        ctxs = [c for c in (build(i, d) for i, d, _ in picks) if c]
        jobs = [(a, c) for c in ctxs for a in self.analysts]
        with ThreadPoolExecutor(max_workers=8) as pool:  # analysts are independent -> run concurrently
            mfut = pool.submit(self.market_researcher.analyse, {"today": today, "market": mkt_data})
            futs = [(c["symbol"], a.name, pool.submit(a.analyse, c)) for a, c in jobs]
            reports: dict[str, list[dict]] = {c["symbol"]: [] for c in ctxs}
            for sym, name, f in futs:
                try:
                    reports[sym].append(f.result().model_dump())
                except Exception as e:
                    log.warning("%s failed for %s: %s", name, sym, e)
            market = mfut.result().model_dump()

        candidates = [{"symbol": c["symbol"], "quote": c["quote"], "setup_signal": signals[c["symbol"]].dict(),
                       "reports": reports[c["symbol"]]} for c in ctxs]
        dec = self.jakad.decide(
            candidates=candidates, market=market, open_positions=self._open_view(),
            playbook=self.playbook(), lessons=self.j.active_lessons(), stats=self.j.stats_by_setup(),
            book=self.book.summary(), scorecard=self.j.analyst_scorecard(), strategy=asdict(self.strategy))
        self.j.event("decision", dec.model_dump_json())
        if dec.action == "SKIP":
            return {"action": "SKIP", "why": dec.skip_reason}
        return self.execute(dec, reports.get(dec.symbol, []), market, signals.get(dec.symbol),
                            (quotes.get(dec.symbol) or {}).get("ltp"))

    # ---- execution --------------------------------------------------------
    def execute(self, dec, reports: list[dict], market: dict, sig=None, ltp: float | None = None) -> dict:
        if sig is None:
            return {"action": "REJECTED", "why": f"{dec.symbol} is not a setup signal"}
        # Levels are the owner's rules, not the LLM's: entry = live price, SL = below swing low, setup = signal kind.
        entry = ltp or sig.close
        if entry > sig.close * (1 + self.s.chase_limit_pct):
            return {"action": "REJECTED", "why": f"price {entry} ran >{100*self.s.chase_limit_pct:.1f}% above signal close {sig.close}"}
        dec.entry_price, dec.stop_loss, dec.setup = entry, sig.sl, sig.kind
        sized = self.book.size_trade(dec.symbol, dec.entry_price, dec.stop_loss, dec.target, dec.risk_pct,
                                    dec.setup, dec.confidence)
        if not sized.ok:
            self.j.event("rejected", f"{dec.symbol}: {sized.reason}")
            return {"action": "REJECTED", "why": sized.reason}
        inst = self.data.resolve(dec.symbol)
        fill, oid = self.exec.buy(inst, sized.quantity, dec.entry_price)
        sl_oid = self.exec.protective_sl(inst, sized.quantity, dec.stop_loss)
        tid = self.j.open_trade(
            mode=self.exec.name, symbol=dec.symbol, security_id=inst.security_id, setup=dec.setup,
            entry_reason=dec.entry_reason, entry_price=fill, quantity=sized.quantity, stop_loss=dec.stop_loss,
            target=dec.target, confidence=dec.confidence, sl_order_id=sl_oid, bos_level=sig.bos_level,
            analyst_reports={"stock": reports, "market": market}, decision=dec.model_dump())
        log.info("OPENED #%s %s x%s @ %.2f SL %.2f TGT %.2f", tid, dec.symbol, sized.quantity, fill, dec.stop_loss, dec.target)
        return {"action": "TRADE", "trade_id": tid, "symbol": dec.symbol, "qty": sized.quantity, "entry": fill}

    def _open_view(self) -> list[dict]:
        return [{k: t[k] for k in ("id", "symbol", "setup", "entry_price", "quantity", "stop_loss", "target", "entry_time")}
                for t in self.j.open_trades()]

    # ---- monitoring: hard SL/target checks + Jakadbangdu position review ---
    def monitor(self, review: bool = False) -> list[str]:
        trades = self.j.open_trades()
        if not trades:
            return []
        insts = {t["symbol"]: Instrument(t["symbol"], t["security_id"]) for t in trades}
        quotes = self.data.quotes(list(insts.values()))
        log_lines = []
        for t in trades:
            q = quotes.get(t["symbol"])
            if not q:
                continue
            px = q["ltp"]
            if px <= t["stop_loss"]:
                log_lines.append(self.close(t, px, "STOP_LOSS hit"))
            elif self.s.exit_at_target and px >= t["target"]:
                log_lines.append(self.close(t, px, "TARGET hit"))
            elif review:
                try:
                    tech = ind.snapshot(self.data.daily(insts[t["symbol"]], 250))
                    r = self.jakad.review_position(trade=dict(t) | {"analyst_reports": None, "decision": None},
                                                   quote=q, technical=tech, playbook=self.playbook(),
                                                   lessons=self.j.active_lessons(10))
                    if r.action == "EXIT":
                        log_lines.append(self.close(t, px, f"DISCRETIONARY EXIT: {r.reason}"))
                    elif r.action == "TRAIL":
                        new_sl = r.new_stop_loss if t["stop_loss"] < r.new_stop_loss < px else None  # never lower a stop
                        new_tgt = r.new_target if r.new_target > px else None
                        if new_sl or new_tgt:
                            sl_oid = None
                            if new_sl and t["sl_order_id"]:
                                self.exec.cancel(t["sl_order_id"])
                                sl_oid = self.exec.protective_sl(insts[t["symbol"]], t["quantity"], new_sl)
                            self.j.update_levels(t["id"], new_sl, new_tgt, sl_oid)
                            log_lines.append(f"TRAIL #{t['id']} {t['symbol']} SL->{new_sl} TGT->{new_tgt}: {r.reason}")
                except Exception as e:
                    log.warning("review %s failed: %s", t["symbol"], e)
        return log_lines

    def close(self, t, px: float, reason: str) -> str:
        inst = Instrument(t["symbol"], t["security_id"])
        if t["sl_order_id"]:
            self.exec.cancel(t["sl_order_id"])
        fill = self.exec.sell(inst, t["quantity"], px)
        self.j.close_trade(t["id"], fill, reason)
        self.learn(t["id"])
        c = self.j.get(t["id"])
        return f"CLOSED #{t['id']} {t['symbol']} @ {fill:.2f} pnl {c['pnl']:.0f} ({c['r_multiple']}R): {reason}"

    # ---- learning loop ----------------------------------------------------
    def learn(self, trade_id: int):
        """Post-mortem -> lesson -> (periodically) playbook rewrite. Failures here never block trading."""
        try:
            rev = self.jakad.reflect(dict(self.j.get(trade_id)))
            self.j.set_review(trade_id, rev.model_dump())
            self.score_analysts(trade_id)
            if rev.rule_for_future.strip():
                self.j.add_lesson(trade_id, rev.mistake_type, rev.what_went_wrong, rev.rule_for_future)
            n = self.j.closed_count()
            if n % CONSOLIDATE_EVERY == 0:
                self.consolidate()
            if n % self.s.review_every_n_trades == 0:
                self.review_strategy()
        except Exception as e:
            log.warning("learning step failed for #%s: %s", trade_id, e)

    def consolidate(self):
        lessons = self.j.active_lessons(100)
        if not lessons:
            return
        c = self.jakad.consolidate(self.playbook(), lessons, self.j.stats_by_setup())
        self.s.playbook_path.parent.mkdir(parents=True, exist_ok=True)
        self.s.playbook_path.write_text(c.playbook_markdown)
        self.j.retire_lessons(c.retire_lesson_ids)

    def score_analysts(self, trade_id: int):
        """Grade each analyst's stance against the outcome so unreliable ones lose weight."""
        t = self.j.get(trade_id)
        reps = json.loads(t["analyst_reports"] or "{}").get("stock", [])
        won = t["pnl"] > 0
        rows = [(r["analyst"], r["stance"], r["conviction"],
                 None if r["stance"] == "NEUTRAL" else int((r["stance"] == "BULLISH") == won)) for r in reps]
        if rows:
            self.j.record_analyst_scores(trade_id, rows)

    def review_strategy(self):
        """Measured results -> enforced parameter changes (risk cap, RR floor, min confidence, avoid-list)."""
        u = self.jakad.strategy_review(report=self.perf.report(), current=asdict(self.strategy),
                                       playbook=self.playbook(), lessons=self.j.active_lessons(20))
        self.strategy = strat.apply_update(self.s, self.strategy, u)
        self.book.strategy = self.strategy
        self.j.log_strategy(u.model_dump())
        log.info("strategy updated: %s", asdict(self.strategy))
        return u

    # ---- structure management: trailing SL + BoS pyramid adds --------------
    def manage_structure(self) -> list[str]:
        """Run on closed daily candles (a few times a day). Trails the stop to each newer higher swing low, and
        adds on a break of structure (close > last swing high) while the risk budget allows."""
        out = []
        for t in self.j.open_trades():
            try:
                inst = Instrument(t["symbol"], t["security_id"])
                d = setups.completed(self.data.daily(inst, 250))
                px = self.data.quotes([inst])[t["symbol"]]["ltp"]
                ref = pd.Timestamp(t["ref_date"] or t["entry_time"])
                # 1) trail: newest confirmed swing low after entry/last add; stops only move up, always below price
                new_sl = setups.trail_level(d, self.s, pd.Timestamp(t["entry_time"]))
                sl = t["stop_loss"]
                if new_sl and sl < new_sl < px:
                    oid = None
                    if t["sl_order_id"]:
                        self.exec.cancel(t["sl_order_id"])
                        oid = self.exec.protective_sl(inst, t["quantity"], new_sl)
                    self.j.update_levels(t["id"], new_sl, None, oid)
                    out.append(f"TRAIL #{t['id']} {t['symbol']} SL {sl} -> {new_sl} (higher swing low)")
                    sl = new_sl
                # 2) BoS add
                lvl = t["bos_level"] or setups.bos_level_after(d, self.s, ref)
                if lvl and t["bos_level"] is None:
                    self.j.set_bos_level(t["id"], lvl)
                if lvl and float(d["close"].iloc[-1]) > lvl and px > t["entry_price"]:
                    sized = self.book.size_add(t, px, sl)
                    if not sized.ok:
                        out.append(f"NO ADD #{t['id']} {t['symbol']}: {sized.reason}")
                        continue
                    fill, _ = self.exec.buy(inst, sized.quantity, px)
                    new_oid = None
                    if t["sl_order_id"]:
                        self.exec.cancel(self.j.get(t["id"])["sl_order_id"])
                        new_oid = self.exec.protective_sl(inst, t["quantity"] + sized.quantity, sl)
                    self.j.add_to_trade(t["id"], fill, sized.quantity, sl, new_oid, ref_date=str(d.index[-1]))  # next BoS must come from a swing formed after this bar
                    self.j.event("add", f"#{t['id']} {t['symbol']} +{sized.quantity} @ {fill} on BoS above {lvl}")
                    out.append(f"ADD #{t['id']} {t['symbol']} +{sized.quantity} @ {fill:.2f} on BoS above {lvl}, SL {sl}")
            except Exception as e:
                log.warning("manage %s failed: %s", t["symbol"], e)
        return out
