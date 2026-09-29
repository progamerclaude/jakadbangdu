import zlib

import numpy as np
import pandas as pd
import pytest

from jakadbangdu import indicators as ind
from jakadbangdu.broker import Instrument, PaperExecutor
from jakadbangdu.config import Settings
from jakadbangdu.engine import Engine
from jakadbangdu.journal import Journal
from jakadbangdu.risk import Book
from jakadbangdu.schemas import AnalystReport, Consolidated, Decision, MarketReport, StrategyUpdate, TradeReview
from jakadbangdu import strategy as strat


def frame(n=320, drift=0.001, seed=1):
    r = np.random.default_rng(seed)
    c = 100 * np.cumprod(1 + drift + r.normal(0, 0.01, n))
    idx = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=n)
    return pd.DataFrame({"open": c * 0.998, "high": c * 1.01, "low": c * 0.99, "close": c,
                         "volume": r.integers(1e5, 2e5, n).astype(float)}, index=idx)


class FakeData:
    NIFTY, BANKNIFTY = Instrument("NIFTY 50", "13", "IDX_I"), Instrument("BANK NIFTY", "25", "IDX_I")

    def __init__(self):
        self.px = {}

    def resolve(self, s):
        return Instrument(s, str(zlib.crc32(s.encode()) % 9999))

    def daily(self, inst, days=300):
        return frame(drift=0.003, seed=zlib.crc32(inst.symbol.encode()) % 50)

    def intraday(self, inst, interval=15, days=5):
        return frame(100)

    def quotes(self, insts):
        return {i.symbol: {"ltp": self.px.get(i.symbol, float(self.daily(i)["close"].iloc[-1])),
                           "open": 1, "high": 1, "low": 1, "prev_close": 1} for i in insts}


class FakeAnalyst:
    name = "Fake"

    def analyse(self, ctx):
        return AnalystReport(analyst="Fake", stance="BULLISH", conviction=70, summary="s", key_points=[], risks=[])


class FakeMarket:
    def analyse(self, ctx):
        return MarketReport(regime="RISK_ON", conviction=60, summary="s", key_points=[], risks=[],
                            sector_leaders=[], sector_laggards=[], event_risks=[])


class FakeJakad:
    def __init__(self, entry=100.0, sl=95.0, tgt=110.0, risk=0.01):
        self.d = dict(entry=entry, sl=sl, tgt=tgt, risk=risk)
        self.lessons_seen = None

    def decide(self, *, candidates, lessons, **kw):
        self.lessons_seen = lessons
        c = candidates[0]
        e = c["quote"]["ltp"]
        return Decision(action="TRADE", symbol=c["symbol"], setup="pullback", entry_price=e, stop_loss=e * .95,
                        target=e * 1.10, risk_pct=self.d["risk"], confidence=70, entry_reason="test")

    def reflect(self, trade):
        return TradeReview(outcome_quality="BAD_PROCESS_BAD_RESULT", mistake_type="stop-too-tight",
                           what_went_wrong="w", what_went_right="r", rule_for_future="Use 1.5x ATR stops",
                           blame_analyst="none")

    def strategy_review(self, **kw):
        return StrategyUpdate(assessment="a", recurring_mistakes=["stop-too-tight"], avoid_setups=["Pullback"],
                              favor_setups=[], risk_pct_cap=0.10, min_reward_risk=0.5, min_confidence=95,
                              analyst_guidance=[], changes_and_rationale="c")

    def consolidate(self, pb, lessons, stats):
        return Consolidated(playbook_markdown="1. Use 1.5x ATR stops", retire_lesson_ids=[l["id"] for l in lessons])


@pytest.fixture
def eng(tmp_path):
    s = Settings(db_path=tmp_path / "j.db", playbook_path=tmp_path / "pb.md", strategy_path=tmp_path / "st.json", shortlist_size=8)
    e = Engine(s, FakeData(), PaperExecutor(), Journal(s.db_path), jakad=FakeJakad(),
               analysts=[FakeAnalyst()], market_researcher=FakeMarket())
    return e


def test_indicators_sane():
    sn = ind.snapshot(frame())
    assert 0 <= sn["rsi14"] <= 100 and sn["atr14"] > 0 and sn["ema200"] is not None


def test_risk_rules(eng):
    b = eng.book
    ok = b.size_trade("A", 100, 95, 110, 0.01)
    assert ok.ok and ok.risk_amount <= 5000 + 100
    assert not b.size_trade("A", 100, 105, 110, 0.01).ok       # SL above entry
    assert not b.size_trade("A", 100, 95, 101, 0.01).ok        # RR < 1


def _fill(eng, syms, risk):
    for i, sym in enumerate(syms):
        s = eng.book.size_trade(sym, 1000, 900, 1300, risk)
        if not s.ok:
            return s
        eng.j.open_trade(mode="paper", symbol=sym, security_id=str(i), setup="x", entry_reason="r", entry_price=1000,
                         quantity=s.quantity, stop_loss=900, target=1300, confidence=1, analyst_reports={}, decision={})
        assert eng.book.free >= 0.30 * eng.book.capital - 1e-6   # never below 30% free
    return None


def test_free_capital_floor(eng):
    eng.book.strategy.risk_pct_cap = 0.02        # allow big positions so the floor binds
    refused = _fill(eng, "ABCDE", 0.02)          # floor stops us before 5
    assert refused and "no room" in refused.reason
    assert eng.book.free >= 0.30 * eng.book.capital - 1e-6


def test_max_five_positions(eng):
    assert _fill(eng, "ABCDE", 0.002) is None    # small positions: all 5 fit
    refused = eng.book.size_trade("F", 1000, 900, 1300, 0.002)
    assert not refused.ok and "max 5" in refused.reason


def test_full_loop_trade_exit_learn(eng):
    res = eng.scan(["AAA", "BBB", "CCC", "DDD"])
    assert res["action"] == "TRADE", res
    t = eng.j.open_trades()[0]
    assert t["entry_reason"] and t["setup"] == "pullback" and t["stop_loss"] < t["entry_price"] < t["target"]
    eng.data.px[t["symbol"]] = t["stop_loss"] - 0.01           # hit the stop
    lines = eng.monitor()
    assert "STOP_LOSS" in lines[0]
    c = eng.j.get(t["id"])
    assert c["status"] == "CLOSED" and c["pnl"] < 0 and c["exit_reason"] and c["review"]
    assert eng.j.active_lessons()[0]["rule"] == "Use 1.5x ATR stops"
    eng.consolidate()
    assert "ATR" in eng.playbook() and not eng.j.active_lessons()
    csv = eng.j.export_csv(eng.s.db_path.parent / "t.csv")
    assert "exit_reason" in csv.read_text().splitlines()[0]


def _closed(eng, pnl, symbol="Z"):
    tid = eng.j.open_trade(mode="paper", symbol=symbol, security_id="1", setup="s", entry_reason="r", entry_price=1000,
                           quantity=100, stop_loss=950, target=1100, confidence=1, decision={},
                           analyst_reports={"stock": [{"analyst": "Technical Analyst", "stance": "BULLISH", "conviction": 80}]})
    eng.j.close_trade(tid, 1000 + pnl / 100, "x")
    return tid


def test_drawdown_breakers_and_no_chasing(eng):
    assert eng.perf.risk_multiplier()[0] == 1.0
    _closed(eng, -22_000)                                    # -4.4% this month -> risk halved
    assert eng.perf.risk_multiplier()[0] == 0.5
    full = 0.01 * 500_000 / 5
    assert eng.book.size_trade("A", 100, 95, 110, 0.01).quantity <= int(full / 2) + 1
    _closed(eng, -20_000)                                    # -8.4% -> halted
    r = eng.book.size_trade("A", 100, 95, 110, 0.01)
    assert not r.ok and "halted" in r.reason


def test_target_hit_locks_in(eng):
    _closed(eng, 55_000)                                     # +11% -> risk halved, not increased
    assert eng.perf.risk_multiplier()[0] == 0.5
    assert eng.perf.month()["status"] == "AHEAD"


def test_analyst_scoring_and_strategy_enforced(eng):
    tid = _closed(eng, -3_000)
    eng.score_analysts(tid)
    sc = eng.j.analyst_scorecard()[0]
    assert sc["analyst"] == "Technical Analyst" and sc["hits"] == 0   # bullish call on a loser
    cur_cap = eng.strategy.risk_pct_cap
    eng.review_strategy()
    st = eng.strategy
    assert st.risk_pct_cap <= min(eng.s.max_risk_per_trade_pct, cur_cap * 1.25)   # LLM asked 10%; clamped
    assert st.min_reward_risk >= eng.s.min_reward_risk and st.min_confidence == 90
    assert "pullback" in st.avoid_setups
    assert not eng.book.size_trade("A", 100, 95, 110, 0.01, setup="Pullback", confidence=99).ok   # avoid-list enforced
    assert not eng.book.size_trade("A", 100, 95, 110, 0.01, setup="breakout", confidence=50).ok   # min confidence enforced
    assert eng.strategy.__class__ is strat.Strategy and eng.s.strategy_path.exists()
    rep = eng.perf.report()
    assert rep["all_time"]["trades"] == 1 and rep["month"]["target_pct"] == 10.0
