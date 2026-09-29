"""Pure pandas/numpy indicators. Input: DataFrame with open/high/low/close/volume."""
import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def macd(close: pd.Series):
    line = ema(close, 12) - ema(close, 26)
    sig = ema(line, 9)
    return line, sig, line - sig


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up, dn = df["high"].diff(), -df["low"].diff()
    plus = np.where((up > dn) & (up > 0), up, 0.0)
    minus = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = atr(df, n)
    pdi = 100 * pd.Series(plus, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / a
    mdi = 100 * pd.Series(minus, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean().fillna(0.0)


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    m, sd = close.rolling(n).mean(), close.rolling(n).std()
    return m - k * sd, m, m + k * sd


def swing_levels(df: pd.DataFrame, lookback: int = 60) -> dict:
    w = df.tail(lookback)
    return {
        "swing_high": round(float(w["high"].max()), 2),
        "swing_low": round(float(w["low"].min()), 2),
        "high_20d": round(float(df["high"].tail(20).max()), 2),
        "low_20d": round(float(df["low"].tail(20).min()), 2),
    }


def snapshot(df: pd.DataFrame) -> dict:
    """Compact indicator readout for the latest bar (what analysts get as evidence)."""
    c = df["close"]
    lo, mid, hi = bollinger(c)
    line, sig, hist = macd(c)
    last = float(c.iloc[-1])
    a = float(atr(df).iloc[-1])
    out = {
        "close": round(last, 2),
        "ema9": round(float(ema(c, 9).iloc[-1]), 2),
        "ema21": round(float(ema(c, 21).iloc[-1]), 2),
        "ema50": round(float(ema(c, 50).iloc[-1]), 2),
        "ema200": round(float(ema(c, 200).iloc[-1]), 2) if len(c) >= 200 else None,
        "rsi14": round(float(rsi(c).iloc[-1]), 1),
        "macd": round(float(line.iloc[-1]), 3),
        "macd_signal": round(float(sig.iloc[-1]), 3),
        "macd_hist": round(float(hist.iloc[-1]), 3),
        "adx14": round(float(adx(df).iloc[-1]), 1),
        "atr14": round(a, 2),
        "atr_pct": round(100 * a / last, 2),
        "bb_lower": round(float(lo.iloc[-1]), 2),
        "bb_upper": round(float(hi.iloc[-1]), 2),
        "vol_ratio_20d": round(float(df["volume"].iloc[-1] / max(df["volume"].tail(20).mean(), 1)), 2),
    }
    out.update(swing_levels(df))
    return out
