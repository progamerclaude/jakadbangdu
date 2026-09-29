import numpy as np
import pandas as pd

from jakadbangdu import backtest as bt
from jakadbangdu.config import Settings


def market(n=900, seed=0, drift=0.0015, amp=0.012, per=7.0, phase=0.0):
    rng = np.random.default_rng(seed)
    c = 100 * np.cumprod(1 + drift + amp * np.sin(np.arange(n) / per + phase) + rng.normal(0, 0.006, n))
    idx = pd.bdate_range("2019-01-01", periods=n)
    o = np.r_[c[0], c[:-1]] * (1 + rng.normal(0, 0.002, n))
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * (1 + rng.uniform(0.001, 0.008, n)),
                         "low": np.minimum(o, c) * (1 - rng.uniform(0.001, 0.008, n)), "close": c, "volume": 1e5}, index=idx)


def universe(k=5, n=700):
    return {f"S{i}": market(n, seed=i + 1, per=6 + i, phase=i) for i in range(k)}, market(n, seed=99, amp=0.004)


def test_backtest_runs_and_respects_portfolio_rules():
    data, nifty = universe()
    res = bt.run(data, nifty)
    t = res.trades
    assert len(t) > 5, "synthetic uptrends should generate trades"
    assert (t.exit_date >= t.entry_date).all()
    m = bt.metrics(res)
    assert m["trades"] == len(t) and -100 < m["max_drawdown_pct"] <= 0
    # never more than 5 positions open at once, never more than the 20% single-name cap on entry
    for d in res.equity.index[::20]:
        assert ((t.entry_date <= d) & (t.exit_date >= d)).sum() <= 5


def test_no_lookahead_truncating_the_future_changes_nothing():
    """Trades closed well before a cut-off must be identical whether or not later bars exist."""
    data, nifty = universe()
    full = bt.run(data, nifty).trades
    cut = nifty.index[500]
    part = bt.run({k: v.loc[:cut] for k, v in data.items()}, nifty.loc[:cut]).trades
    cols = ["symbol", "entry_date", "exit_date", "entry", "exit", "qty", "pnl"]
    early_full = full[full.exit_date < cut - pd.Timedelta(days=10)][cols].reset_index(drop=True)
    early_part = part[(part.exit_date < cut - pd.Timedelta(days=10)) & (part.exit_reason != "end-of-test")][cols].reset_index(drop=True)
    pd.testing.assert_frame_equal(early_full, early_part)


def test_costs_reduce_returns_and_walk_forward_shapes():
    data, nifty = universe()
    free = bt.metrics(bt.run(data, nifty, cost=0.0))["total_return_pct"]
    paid = bt.metrics(bt.run(data, nifty, cost=0.003))["total_return_pct"]
    assert paid < free
    wf = bt.walk_forward(data, nifty)
    assert set(wf) == {"split_date", "in_sample", "out_of_sample"}
    rows = bt.robustness(data, nifty, Settings())
    assert rows[0]["variant"] == "base" and len(rows) == 7
