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
from jakadbangdu import setups
from jakadbangdu import strategy as strat


def frame(n=320, drift=0.001, seed=1):
    r = np.random.default_rng(seed)
    c = 100 * np.cumprod(1 + drift + r.normal(0, 0.01, n))
    idx = pd.bdate_range(end=pd.Timestamp.now().normalize(), periods=n)
    return pd.DataFrame({"open": c * 0.998, "high": c * 1.01, "low": c * 0.99, "close": c,
                         "volume": r.integers(1e5, 2e5, n).astype(float)}, index=idx)


def build(pull=(), tail=(), drift=0.002, amp=0.008, per=8.0, seed=1):
    """Wobbling uptrend, then `pull` multipliers (pullback), then `tail` multipliers (later price action)."""
    rng = np.random.default_rng(seed)
    c = [100.0]
    for i in range(220):
        c.append(c[-1] * (1 + drift + amp * np.sin(i / per)))
    for k in tuple(pull) + tuple(tail):
        c.append(c[-1] * k)
    c = np.array(c)
    # anchored start so that build(PULL) ends yesterday and longer tails extend past it without shifting earlier bars
    start = pd.bdate_range(end=pd.Timestamp.now().normalize() - pd.Timedelta(days=1), periods=226)[0]
    idx = pd.bdate_range(start=start, periods=len(c))
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * (1 + rng.uniform(0.001, 0.006, len(c))),
                         "low": np.minimum(o, c) * (1 - rng.uniform(0.001, 0.006, len(c))), "close": c,
                         "volume": 1e5}, index=idx)


PULL = (0.99,) * 5
RALLY_DIP_RALLY = (1.015,) * 6 + (0.99,) * 4 + (1.015,) * 5


class FakeData:
    NIFTY, BANKNIFTY = Instrument("NIFTY 50", "13", "IDX_I"), Instrument("BANK NIFTY", "25", "IDX_I")

    def __init__(self):
        self.px = {}

    def resolve(self, s):
        return Instrument(s, str(zlib.crc32(s.encode()) % 9999))

    nifty_up = True
    aaa_tail = ()

    def daily(self, inst, days=300):
        if inst.symbol == "NIFTY 50":
            return build(drift=0.002 if self.nifty_up else -0.002, amp=0.002)
        if inst.symbol == "AAA":
            return build(PULL, self.aaa_tail)
        return build(drift=0.0, amp=0.02, seed=5)          # sideways: no stack, never a signal

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
        e, sl = c["quote"]["ltp"], c["setup_signal"]["sl"]
        return Decision(action="TRADE", symbol=c["symbol"], setup="llm-made-up", entry_price=e * 1.2, stop_loss=e * .5,
                        target=e + 3 * (e - sl), risk_pct=self.d["risk"], confidence=70, entry_reason="test")

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
    eng.s.__class__  # settings frozen; nothing to toggle
    res = eng.scan(["AAA", "BBB", "CCC", "DDD"])
    assert res["action"] == "TRADE", res
    t = eng.j.open_trades()[0]
    assert t["entry_reason"] and t["setup"].startswith("stack-pullback")      # setup name comes from the rule, not the LLM
    assert t["stop_loss"] < t["entry_price"] < t["target"]
    assert abs(t["entry_price"] - eng.data.quotes([Instrument("AAA", "")])["AAA"]["ltp"]) < 1e-6   # LLM's 1.2x entry ignored
    assert t["stop_loss"] > t["entry_price"] * 0.85                                 # structural SL, not the LLM's 0.5x
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


def test_setup_signal_rules(eng):
    s = eng.s
    sg = setups.pullback_signal(build(PULL), s, "AAA")
    assert sg and sg.kind in ("stack-pullback-20", "stack-pullback-40")
    assert sg.sl < sg.swing_low < sg.close                       # SL below the last swing low
    assert abs(sg.sl - (sg.swing_low - s.sl_buffer_atr * sg.atr)) < 0.02
    m = setups.mas(build(PULL), s)
    assert m[20].iloc[-1] > m[40].iloc[-1] > m[89].iloc[-1] > m[100].iloc[-1]   # stack holds
    assert setups.pullback_signal(build(drift=0.0, amp=0.02, seed=5), s) is None    # no stack -> no signal
    assert setups.pullback_signal(build((0.95,) * 12), s) is None                   # collapsed through MA40


def test_regime_filter_is_optional(tmp_path):
    def make(flag):
        s = Settings(db_path=tmp_path / f"{flag}.db", playbook_path=tmp_path / "pb.md", strategy_path=tmp_path / "st.json",
                     require_market_uptrend=flag)
        e = Engine(s, FakeData(), PaperExecutor(), Journal(s.db_path), jakad=FakeJakad(),
                   analysts=[FakeAnalyst()], market_researcher=FakeMarket())
        e.data.nifty_up = False                                  # NIFTY in a downtrend
        return e
    on = make(True).scan(["AAA"])
    assert on["action"] == "SKIP" and "regime" in on["why"]      # filter on: blocked
    off = make(False)
    assert off.scan(["AAA"])["action"] == "TRADE"                # default: stock-level rules decide, NIFTY ignored
    assert Settings().require_market_uptrend is False


def test_trail_and_bos_add(eng):
    assert eng.scan(["AAA", "BBB"])["action"] == "TRADE"
    t0 = dict(eng.j.open_trades()[0])
    entry_bar = build(PULL).index[-1]
    eng.j.db.execute("UPDATE trades SET entry_time=?, ref_date=? WHERE id=?", (str(entry_bar), str(entry_bar), t0["id"]))
    eng.j.db.commit()
    eng.data.aaa_tail = RALLY_DIP_RALLY                          # rally, higher-low dip, then break above prior peak
    out = eng.manage_structure()
    t1 = eng.j.get(t0["id"])
    assert any(l.startswith("TRAIL") for l in out), out
    assert t1["stop_loss"] > t0["stop_loss"]                     # stop only moved up
    assert any(l.startswith("ADD") for l in out), out
    assert t1["adds"] == 1 and t1["quantity"] > t0["quantity"] and t1["quantity"] <= t0["quantity"] * 1.5 + 1
    assert t1["risk_amount"] == t0["risk_amount"] and t1["initial_stop_loss"] == t0["initial_stop_loss"]
    assert t1["entry_price"] > t0["entry_price"]                 # weighted average moved up
    assert (t1["entry_price"] - t1["stop_loss"]) * t1["quantity"] <= t0["risk_amount"] + 1   # total risk <= original 1R
    assert eng.book.free >= 0.30 * eng.book.capital - 1e-6
    again = eng.manage_structure()                               # same bar again: no duplicate add
    assert eng.j.get(t0["id"])["adds"] == 1 and not any(l.startswith("ADD") for l in again)


def test_live_guard_and_scheduler(tmp_path, monkeypatch):
    from datetime import datetime
    from jakadbangdu import __main__ as m
    j = Journal(tmp_path / "g.db")
    live = Settings(db_path=tmp_path / "g.db", mode="live")
    with pytest.raises(SystemExit):                       # no confirmation
        m.guard_live(live, j)
    monkeypatch.setenv("JAKAD_CONFIRM_LIVE", m.LIVE_CONFIRM)
    with pytest.raises(SystemExit):                       # confirmed but no paper track record
        m.guard_live(live, j)
    monkeypatch.setenv("JAKAD_MIN_PAPER_TRADES", "0")
    m.guard_live(live, j)                                 # both deliberate acts -> allowed
    m.guard_live(Settings(db_path=tmp_path / "g.db", mode="paper"), j)   # paper never blocked

    done = set()
    wed = lambda h, mi: datetime(2026, 9, 30, h, mi, tzinfo=m.IST)       # a Wednesday
    assert m.due_jobs(wed(9, 30), done) == []
    assert m.due_jobs(wed(9, 46), done) == ["scan"]
    assert m.due_jobs(wed(9, 47), done) == []                            # not repeated
    assert m.due_jobs(wed(14, 5), done) == ["scan"]                      # 12:00 review is 125 min late: skipped, 13:30 scan runs
    assert m.due_jobs(wed(15, 46), done) == ["review", "manage", "audit"]   # audit fires AFTER the close (old loop never ran it)
    assert m.due_jobs(datetime(2026, 10, 3, 9, 50, tzinfo=m.IST), set()) == []   # Saturday


def test_rules_only_mode_needs_no_llm(tmp_path, monkeypatch):
    """Full loop with the deterministic trader: trades the setup, exits on the stop, journals it, never calls Claude."""
    from jakadbangdu import llm
    from jakadbangdu.rules_strategy import NullMarket, RulesTrader
    monkeypatch.setattr(llm, "client", lambda: (_ for _ in ()).throw(AssertionError("LLM called in rules-only mode")))
    s = Settings(db_path=tmp_path / "r.db", playbook_path=tmp_path / "pb.md", strategy_path=tmp_path / "st.json")
    e = Engine(s, FakeData(), PaperExecutor(), Journal(s.db_path), jakad=RulesTrader(), analysts=[], market_researcher=NullMarket())
    assert e.scan(["AAA", "BBB"])["action"] == "TRADE"
    t = e.j.open_trades()[0]
    assert t["setup"].startswith("stack-pullback") and t["confidence"] == 100 and "SL below swing low" in t["entry_reason"]
    assert (t["target"] - t["entry_price"]) >= 2 * (t["entry_price"] - t["stop_loss"]) - 1e-6     # at least 2R
    e.data.px["AAA"] = t["stop_loss"] - 0.01
    assert "STOP_LOSS" in e.monitor()[0]
    c = e.j.get(t["id"])
    assert c["status"] == "CLOSED" and c["review"] and c["exit_reason"] and c["r_multiple"] < 0
    assert e.j.active_lessons() == []                       # mechanical mode invents no lessons
    e.review_strategy()                                     # no-op parameters, no LLM
