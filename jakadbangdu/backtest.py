"""Backtest of the deterministic rules (no LLM): stack-pullback entry, swing-low SL, BoS adds, trailing SL.

No-lookahead discipline: every decision uses data up to the close of day t and is EXECUTED at the open of day t+1;
stops are checked against day t+1's range (gap-downs fill at the open, worse than the stop). Pivots are only used
once confirmed (n bars after). What is NOT modelled: the LLM's take/skip filter (so this is the rule-only upper
bound on signal quality, not the full system), the RR gate (target is the LLM's), circuit-breaker lock-in."""
import math
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from . import setups
from .config import Settings

COST_PER_SIDE = 0.0018      # STT 0.1% + exchange/stamp/GST ~0.03% + slippage ~0.05%, each side (delivery)
WARMUP = 220


@dataclass
class Pos:
    symbol: str
    kind: str
    entry_date: pd.Timestamp
    entry: float          # weighted average
    qty: int
    sl: float
    risk0: float          # original 1R in rupees
    bos: float | None
    ref: pd.Timestamp
    adds: int = 0
    cost: float = 0.0


@dataclass
class Result:
    trades: pd.DataFrame
    equity: pd.Series
    settings: dict = field(default_factory=dict)


def run(data: dict[str, pd.DataFrame], nifty: pd.DataFrame, s: Settings | None = None,
        risk_pct: float = 0.01, cost: float = COST_PER_SIDE, start: str | None = None, end: str | None = None) -> Result:
    s = s or Settings()
    dates = nifty.index
    if start:
        dates = dates[dates >= pd.Timestamp(start)]
    if end:
        dates = dates[dates <= pd.Timestamp(end)]
    # cheap prefilter (causal, precomputed once): only run the full signal on stocks whose MA stack held for 10 bars
    p20, p40, p89, p100 = s.ma_periods
    stacked = {}
    for sym, df in data.items():
        m = setups.mas(df, s)
        ok = (m[p20] > m[p40]) & (m[p40] > m[p89]) & (m[p89] > m[p100]) & (df["close"] > m[p40])
        stacked[sym] = ok.rolling(10, min_periods=10).min().fillna(0).astype(bool) | (ok & ok.shift(1, fill_value=False))
    cash_capital = s.starting_capital     # starting + realized P&L (same accounting as live)
    open_pos: dict[str, Pos] = {}
    pending: list[tuple] = []             # ("enter", sym, sig) / ("add", sym, sl)
    trades, eq = [], {}
    month_key, month_base = None, cash_capital

    def bar(sym, d):
        df = data[sym]
        return df.loc[d] if d in df.index else None

    def deployed():
        return sum(p.entry * p.qty for p in open_pos.values())

    for d in dates:
        i_n = nifty.index.get_loc(d)
        # ---- month bookkeeping / circuit breakers (realized P&L based, like live) --------------------
        mk = d.strftime("%Y-%m")
        if mk != month_key:
            month_key, month_base = mk, cash_capital
        mpnl = (cash_capital - month_base) / month_base
        mult = 0.0 if mpnl <= -s.dd_halt_pct else 0.5 if mpnl <= -s.dd_halve_risk_pct else 1.0

        # ---- 1) execute yesterday's decisions at today's open ---------------------------------------
        for act in pending:
            kind, sym = act[0], act[1]
            b = bar(sym, d)
            if b is None:
                continue
            px = float(b["open"]) * (1 + cost)     # buy: pay up
            if kind == "enter" and sym not in open_pos:
                sig = act[2]
                if len(open_pos) >= s.max_open_positions or mult == 0 or float(b["open"]) > sig.close * (1 + s.chase_limit_pct) \
                        or not sig.sl < float(b["open"]):
                    continue
                cap = cash_capital
                qty = int(cap * min(risk_pct, s.max_risk_per_trade_pct) * mult / (px - sig.sl))
                qty = min(qty, int(cap * s.max_single_position_pct / px))
                spend_room = cap - deployed() - cap * s.min_free_capital_pct
                qty = min(qty, int(spend_room / px)) if spend_room > 0 else 0
                if qty < 1:
                    continue
                open_pos[sym] = Pos(sym, sig.kind, d, px, qty, sig.sl, qty * (px - sig.sl), sig.bos_level, d, cost=qty * float(b["open"]) * cost)
            elif kind == "add" and sym in open_pos:
                p, new_sl = open_pos[sym], act[2]
                if p.adds >= s.max_adds or mult == 0 or not new_sl < px:
                    continue
                budget = p.risk0 * mult - max(0.0, p.entry - new_sl) * p.qty
                cap = cash_capital
                qty = min(int(budget / (px - new_sl)) if budget > 0 else 0, int(p.qty * s.add_fraction),
                          int(cap * s.max_single_position_pct / px) - p.qty)
                room = cap - deployed() - cap * s.min_free_capital_pct
                qty = min(qty, int(room / px)) if room > 0 else 0
                if qty < 1:
                    continue
                p.entry = (p.entry * p.qty + px * qty) / (p.qty + qty)
                p.qty += qty
                p.sl, p.adds, p.bos, p.ref = new_sl, p.adds + 1, None, d
                p.cost += qty * float(b["open"]) * cost
        pending = []

        # ---- 2) stops against today's range -----------------------------------------------------------
        for sym in list(open_pos):
            p, b = open_pos[sym], bar(sym, d)
            if b is None:
                continue
            if float(b["low"]) <= p.sl:
                fill = min(float(b["open"]), p.sl) * (1 - cost)   # gap-down fills at the open
                _close(p, d, fill, "stop", trades)
                cash_capital += trades[-1]["pnl"]
                del open_pos[sym]

        # ---- 3) after the close: trail stops, look for BoS adds, then new signals (executed tomorrow) --
        for sym, p in open_pos.items():
            df = data[sym].loc[:d].tail(400)
            new_sl = setups.trail_level(df, s, p.entry_date)
            close = float(df["close"].iloc[-1])
            if new_sl and p.sl < new_sl < close:
                p.sl = new_sl
            lvl = p.bos or setups.bos_level_after(df, s, p.ref)
            if lvl and p.bos is None:
                p.bos = lvl
            if lvl and close > lvl and close > p.entry and p.adds < s.max_adds:
                pending.append(("add", sym, p.sl))
        slots = s.max_open_positions - len(open_pos)
        nf = nifty.loc[:d].tail(400)
        if slots > 0 and i_n >= WARMUP and (not s.require_market_uptrend or setups.market_uptrend(nf)):
            found = []
            for sym, df in data.items():
                if sym in open_pos or d not in df.index or not stacked[sym].get(d, False):
                    continue
                w = df.loc[:d].tail(400)
                sig = setups.pullback_signal(w, s, sym, nf)
                if sig:
                    found.append((sig.rs60, sym, sig))
            found.sort(key=lambda x: -x[0])
            pending += [("enter", sym, sig) for _, sym, sig in found[:slots]]

        # ---- mark to market ---------------------------------------------------------------------------
        mtm = sum((float(data[sy].loc[:d]["close"].iloc[-1]) - p.entry) * p.qty for sy, p in open_pos.items())
        eq[d] = cash_capital + mtm

    for sym, p in list(open_pos.items()):   # close what's left at the last close so the tally is complete
        last = data[sym].loc[:dates[-1]]
        _close(p, dates[-1], float(last["close"].iloc[-1]) * (1 - cost), "end-of-test", trades)
    return Result(pd.DataFrame(trades), pd.Series(eq), {"risk_pct": risk_pct, "cost": cost})


def _close(p: Pos, d, fill: float, why: str, trades: list):
    pnl = (fill - p.entry) * p.qty - p.cost   # p.entry already includes buy slippage; p.cost = buy-side fees; fill is net of sell cost
    trades.append({"symbol": p.symbol, "setup": p.kind, "entry_date": p.entry_date, "exit_date": d, "entry": round(p.entry, 2),
                   "exit": round(fill, 2), "qty": p.qty, "adds": p.adds, "pnl": round(pnl, 2),
                   "r": round(pnl / p.risk0, 2) if p.risk0 else None, "exit_reason": why})


def metrics(res: Result, start_capital: float = 500_000.0) -> dict:
    t, eq = res.trades, res.equity
    if t.empty:
        return {"trades": 0}
    m = eq.resample("ME").last()
    prev = pd.concat([pd.Series([start_capital]), m.iloc[:-1]]).values
    mret = (m.values / prev - 1)
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    dd = (eq / eq.cummax() - 1).min()
    w, l = t[t.pnl > 0].pnl.sum(), -t[t.pnl <= 0].pnl.sum()
    return {"trades": len(t), "win_rate": round((t.pnl > 0).mean(), 2), "expectancy_r": round(t.r.mean(), 2),
            "profit_factor": round(w / l, 2) if l else None, "avg_win_r": round(t[t.pnl > 0].r.mean(), 2) if (t.pnl > 0).any() else 0,
            "avg_loss_r": round(t[t.pnl <= 0].r.mean(), 2) if (t.pnl <= 0).any() else 0,
            "total_return_pct": round(100 * (eq.iloc[-1] / start_capital - 1), 1),
            "cagr_pct": round(100 * ((eq.iloc[-1] / start_capital) ** (1 / years) - 1), 1),
            "avg_month_pct": round(100 * mret.mean(), 2), "median_month_pct": round(100 * float(np.median(mret)), 2),
            "months_ge_10pct": f"{int((mret >= 0.10).sum())}/{len(mret)}", "months_negative": f"{int((mret < 0).sum())}/{len(mret)}",
            "worst_month_pct": round(100 * mret.min(), 1), "max_drawdown_pct": round(100 * dd, 1),
            "adds_taken": int(t.adds.sum()), "stop_exits": int((t.exit_reason == "stop").sum())}


def walk_forward(data, nifty, s=None, split: float = 0.6, **kw) -> dict:
    """Design on the first part, judge on the untouched rest: the number that matters is the out-of-sample one."""
    idx = nifty.index[WARMUP:]
    cut = idx[int(len(idx) * split)]
    ins = run(data, nifty, s, start=str(idx[0].date()), end=str(cut.date()), **kw)
    oos = run(data, nifty, s, start=str((cut + pd.Timedelta(days=1)).date()), **kw)
    return {"split_date": str(cut.date()), "in_sample": metrics(ins), "out_of_sample": metrics(oos)}


def robustness(data, nifty, s: Settings, **kw) -> list[dict]:
    """Does the edge survive nearby parameters, or is it one lucky setting? (tolerance, MA type, stop buffer)."""
    rows = []
    for label, cfg in (("base", s), ("sma", replace(s, ma_type="sma")), ("tol 2%", replace(s, pullback_tol=0.02)),
                       ("tol 0.5%", replace(s, pullback_tol=0.005)), ("with NIFTY>EMA50 filter", replace(s, require_market_uptrend=True)),
                       ("wider SL buffer 0.3 ATR", replace(s, sl_buffer_atr=0.3)), ("no adds", replace(s, max_adds=0))):
        rows.append({"variant": label} | {k: v for k, v in metrics(run(data, nifty, cfg, **kw)).items()
                                          if k in ("trades", "expectancy_r", "profit_factor", "cagr_pct", "max_drawdown_pct")})
    return rows
