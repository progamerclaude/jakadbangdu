"""Orchestration: scan -> analyst panel (parallel) -> Jakadbangdu -> risk gate -> execute -> monitor -> learn."""
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import indicators as ind
from .agents.analysts import MarketResearcher, stock_analysts
from .broker import DhanData, Instrument
from .config import Settings
from .jakadbangdu import Jakadbangdu
from .journal import Journal
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
        self.book = Book(s, journal)

    # ---- playbook memory --------------------------------------------------
    def playbook(self) -> str:
        p = self.s.playbook_path
        return p.read_text() if p.exists() else ""

    # ---- screening (cheap, code-only; keeps LLM spend on real candidates) --
    def shortlist(self, symbols: list[str]) -> list[tuple[Instrument, "pd.DataFrame", float]]:
        held = {t["symbol"] for t in self.j.open_trades()}
        scored = []
        for sym in symbols:
            if sym in held:
                continue
            try:
                inst = self.data.resolve(sym)
                d = self.data.daily(inst)
                if len(d) < 60:
                    continue
                sn = ind.snapshot(d)
                score = 0.0
                score += 2 if sn["close"] > sn["ema50"] else -2
                score += 1 if sn["ema21"] > sn["ema50"] else -1
                score += 1 if 45 <= sn["rsi14"] <= 68 else (-1 if sn["rsi14"] > 75 or sn["rsi14"] < 35 else 0)
                score += 1 if sn["adx14"] > 20 else 0
                score += 1 if sn["vol_ratio_20d"] > 1.2 else 0
                score += 1 if sn["macd_hist"] > 0 else -1
                scored.append((score, inst, d))
            except Exception as e:  # one bad symbol must not stop the scan
                log.warning("screen %s failed: %s", sym, e)
        scored.sort(key=lambda x: -x[0])
        return [(i, d, sc) for sc, i, d in scored[: self.s.shortlist_size] if sc >= 3]

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

        picks = self.shortlist(symbols)
        if not picks:
            return {"action": "SKIP", "why": "screen found no candidates"}
        nifty = self.data.daily(self.data.NIFTY, 300)
        quotes = self.data.quotes([p[0] for p in picks])
        mkt_data = self.market_context()

        def build(i: Instrument, d):
            q = quotes.get(i.symbol)
            if not q:
                return None
            try:
                intr = self.data.intraday(i, 15, 5)
            except Exception:
                intr = None
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

        candidates = [{"symbol": c["symbol"], "quote": c["quote"], "reports": reports[c["symbol"]]} for c in ctxs]
        dec = self.jakad.decide(
            candidates=candidates, market=market, open_positions=self._open_view(),
            playbook=self.playbook(), lessons=self.j.active_lessons(), stats=self.j.stats_by_setup(),
            book=self.book.summary())
        self.j.event("decision", dec.model_dump_json())
        if dec.action == "SKIP":
            return {"action": "SKIP", "why": dec.skip_reason}
        return self.execute(dec, reports.get(dec.symbol, []), market)

    # ---- execution --------------------------------------------------------
    def execute(self, dec, reports: list[dict], market: dict) -> dict:
        sized = self.book.size_trade(dec.symbol, dec.entry_price, dec.stop_loss, dec.target, dec.risk_pct)
        if not sized.ok:
            self.j.event("rejected", f"{dec.symbol}: {sized.reason}")
            return {"action": "REJECTED", "why": sized.reason}
        inst = self.data.resolve(dec.symbol)
        fill, oid = self.exec.buy(inst, sized.quantity, dec.entry_price)
        sl_oid = self.exec.protective_sl(inst, sized.quantity, dec.stop_loss)
        tid = self.j.open_trade(
            mode=self.exec.name, symbol=dec.symbol, security_id=inst.security_id, setup=dec.setup,
            entry_reason=dec.entry_reason, entry_price=fill, quantity=sized.quantity, stop_loss=dec.stop_loss,
            target=dec.target, confidence=dec.confidence, sl_order_id=sl_oid,
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
            elif px >= t["target"]:
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
            if rev.rule_for_future.strip():
                self.j.add_lesson(trade_id, rev.mistake_type, rev.what_went_wrong, rev.rule_for_future)
            if self.j.closed_count() % CONSOLIDATE_EVERY == 0:
                self.consolidate()
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
