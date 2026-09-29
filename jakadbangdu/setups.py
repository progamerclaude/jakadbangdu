"""The owner's setup, as deterministic code.

Trend stack : MA20 > MA40 > MA89 > MA100 (aligned for the last 10 bars) and price above MA40.
Entry       : pullback that touches MA20 (price still above it) or MA40 (price dipped under MA20 but holds MA40).
Stop        : below the last confirmed swing low (fractal), buffered by a fraction of ATR.
Add         : on Break of Structure (close above the last confirmed swing high), trailing the stop to the newest higher swing low.
"""
from dataclasses import asdict, dataclass

import pandas as pd

from . import indicators as ind
from .config import Settings


def completed(df: pd.DataFrame) -> pd.DataFrame:
    """Drop today's still-forming bar so signals use closed candles only."""
    return df.iloc[:-1] if len(df) and df.index[-1].date() == pd.Timestamp.now().date() else df


def mas(df: pd.DataFrame, s: Settings) -> pd.DataFrame:
    f = ind.ema if s.ma_type == "ema" else (lambda x, n: x.rolling(n).mean())
    return pd.DataFrame({n: f(df["close"], n) for n in s.ma_periods})


def pivots(df: pd.DataFrame, n: int):
    """Confirmed fractal swings only (need n bars after). Returns (lows, highs) as lists of (timestamp, price)."""
    lo, hi = df["low"].values, df["high"].values
    lows, highs = [], []
    for i in range(n, len(df) - n):
        if lo[i] < min(lo[i - n:i]) and lo[i] < min(lo[i + 1:i + n + 1]):
            lows.append((df.index[i], float(lo[i])))
        if hi[i] > max(hi[i - n:i]) and hi[i] > max(hi[i + 1:i + n + 1]):
            highs.append((df.index[i], float(hi[i])))
    return lows, highs


@dataclass
class Signal:
    symbol: str
    kind: str            # stack-pullback-20 | stack-pullback-40
    close: float         # last completed close
    sl: float            # structural stop (below swing low, buffered)
    swing_low: float
    swing_high: float    # reference for target and for the first BoS
    bos_level: float
    atr: float
    stop_pct: float
    rr_to_swing_high: float
    fully_stacked: bool  # price also above MA20
    bullish_close: bool
    adx: float
    rs60: float          # 60d return minus NIFTY's

    def dict(self):
        return asdict(self)


def pullback_signal(df: pd.DataFrame, s: Settings, symbol: str = "", nifty: pd.DataFrame | None = None) -> Signal | None:
    if len(df) < max(s.ma_periods) + 20:
        return None
    m = mas(df, s)
    p20, p40, p89, p100 = s.ma_periods
    aligned = (m[p20] > m[p40]) & (m[p40] > m[p89]) & (m[p89] > m[p100])
    if not aligned.tail(10).all():
        return None
    last, prev = df.iloc[-1], df.iloc[-2]
    c, low = float(last["close"]), float(last["low"])
    e20, e40, tol = float(m[p20].iloc[-1]), float(m[p40].iloc[-1]), s.pullback_tol
    if c <= e40:
        return None  # structure lost
    if c < e20 and low <= e40 * (1 + tol) and c >= e40 * (1 - tol):
        kind = "stack-pullback-40"
    elif c >= e20 * (1 - tol) and low <= e20 * (1 + tol):
        kind = "stack-pullback-20"
    else:
        return None
    if float(df["high"].tail(20).max()) < c * 1.02:
        return None  # not a real pullback: price never extended away from the MA
    lows, highs = pivots(df.tail(120), s.pivot_n)
    if not lows or lows[-1][1] >= c:
        return None  # no swing low, or price already broke the latest one (structure lost)
    swing_low = lows[-1][1]
    a = float(ind.atr(df).iloc[-1])
    sl = swing_low - s.sl_buffer_atr * a
    stop_pct = (c - sl) / c
    if not (0 < stop_pct <= s.max_stop_pct):
        return None
    above = [p for _, p in highs if p > c]
    swing_high = above[-1] if above else float(df["high"].tail(60).max())
    n_ret = lambda x: x["close"].iloc[-1] / x["close"].iloc[-61] - 1 if len(x) > 61 else 0.0
    rs = float(n_ret(df) - (n_ret(nifty) if nifty is not None else 0.0))
    return Signal(symbol, kind, round(c, 2), round(sl, 2), round(swing_low, 2), round(swing_high, 2), round(swing_high, 2),
                  round(a, 2), round(100 * stop_pct, 2), round((swing_high - c) / (c - sl), 2), c > e20,
                  bool(last["close"] > last["open"]), round(float(ind.adx(df).iloc[-1]), 1), round(100 * rs, 1))


def market_uptrend(nifty: pd.DataFrame) -> bool:
    c = nifty["close"]
    return float(c.iloc[-1]) > float(ind.ema(c, 50).iloc[-1])


def trail_level(df: pd.DataFrame, s: Settings, after) -> float | None:
    """Newest confirmed swing low formed after `after`, minus the ATR buffer."""
    lows, _ = pivots(df, s.pivot_n)
    lows = [p for ts, p in lows if ts > after]
    return round(lows[-1] - s.sl_buffer_atr * float(ind.atr(df).iloc[-1]), 2) if lows else None


def bos_level_after(df: pd.DataFrame, s: Settings, after) -> float | None:
    """First confirmed swing high formed after `after` (the level a BoS must clear)."""
    _, highs = pivots(df, s.pivot_n)
    highs = [p for ts, p in highs if ts > after]
    return highs[-1] if highs else None
