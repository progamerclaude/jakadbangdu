import json

import numpy as np

from .. import indicators as ind
from .. import llm
from ..schemas import MarketReport
from .base import Analyst, WebAnalyst


def _j(x) -> str:
    return json.dumps(x, indent=1, default=str)


class TechnicalAnalyst(Analyst):
    name = "Technical Analyst"
    domain = ("Price action and indicators on the daily and 15-minute charts: structure, support/resistance, "
              "momentum, volatility, candlestick context. Propose concrete entry/stop/target from chart levels "
              "(stop beyond structure or ~1-2x ATR, never arbitrary).")

    def evidence(self, ctx):
        d = ctx["daily"]
        return (f"Daily indicators: {_j(ind.snapshot(d))}\n"
                f"15-min indicators: {_j(ind.snapshot(ctx['intraday'])) if ctx.get('intraday') is not None else 'n/a'}\n"
                f"Last 10 daily bars (OHLCV):\n{d.tail(10).round(2).to_string()}\n"
                f"Live quote: {_j(ctx['quote'])}")


class TrendAnalyst(Analyst):
    name = "Trend Analyst"
    domain = ("Trend direction, strength and maturity across timeframes (weekly/daily/intraday), trend regime "
              "(trending vs ranging), and relative strength vs NIFTY 50. Say whether the trend is early, "
              "mature or exhausted.")

    def evidence(self, ctx):
        d, n = ctx["daily"], ctx["nifty"]
        w = d["close"].resample("W").last().dropna()
        rs = (d["close"] / n["close"].reindex(d.index).ffill()).dropna()
        ret = lambda s, k: round(100 * (s.iloc[-1] / s.iloc[-k - 1] - 1), 2) if len(s) > k else None
        return _j({
            "returns_pct": {"5d": ret(d["close"], 5), "20d": ret(d["close"], 20), "60d": ret(d["close"], 60),
                            "nifty_20d": ret(n["close"], 20), "nifty_60d": ret(n["close"], 60)},
            "weekly": {"close": round(float(w.iloc[-1]), 2), "ema10w": round(float(ind.ema(w, 10).iloc[-1]), 2),
                       "ema30w": round(float(ind.ema(w, 30).iloc[-1]), 2), "rsi14w": round(float(ind.rsi(w).iloc[-1]), 1)},
            "daily": {k: v for k, v in ind.snapshot(d).items() if k in ("ema21", "ema50", "ema200", "adx14", "close")},
            "rel_strength_vs_nifty": {"now": round(float(rs.iloc[-1]), 4), "ema20": round(float(ind.ema(rs, 20).iloc[-1]), 4),
                                      "ema50": round(float(ind.ema(rs, 50).iloc[-1]), 4)},
            "pct_from_52w_high": round(100 * (d["close"].iloc[-1] / d["high"].tail(250).max() - 1), 2),
        })


class DataAnalyst(Analyst):
    name = "Data Analyst"
    domain = ("Quantitative, statistical view only: volatility, liquidity, gap behaviour, volume anomalies, "
              "distribution of past forward returns for similar conditions, drawdown profile. Say whether the "
              "numbers support the trade and how wide a stop the volatility demands. You do not read charts "
              "by eye or news; you read the statistics you are given.")

    def evidence(self, ctx):
        d = ctx["daily"]
        c, r = d["close"], d["close"].pct_change().dropna()
        rsi14 = ind.rsi(c)
        fwd5 = c.shift(-5) / c - 1
        similar = fwd5[(rsi14.between(rsi14.iloc[-1] - 5, rsi14.iloc[-1] + 5)) & (c > ind.ema(c, 50))].dropna()
        gaps = (d["open"] / c.shift() - 1).dropna()
        turnover_cr = float((d["close"] * d["volume"]).tail(20).mean() / 1e7)
        return _j({
            "daily_vol_pct_20d": round(100 * float(r.tail(20).std()), 2),
            "daily_vol_pct_1y": round(100 * float(r.tail(250).std()), 2),
            "avg_turnover_cr_20d": round(turnover_cr, 1),
            "volume_ratio_today_vs_20d": ind.snapshot(d)["vol_ratio_20d"],
            "gap_stats_pct": {"mean_abs": round(100 * float(gaps.abs().tail(120).mean()), 2),
                              "worst_gap_down": round(100 * float(gaps.tail(250).min()), 2)},
            "max_drawdown_pct_1y": round(100 * float((c.tail(250) / c.tail(250).cummax() - 1).min()), 2),
            "similar_condition_5d_fwd": {"samples": int(len(similar)),
                                         "win_rate": round(float((similar > 0).mean()), 2) if len(similar) else None,
                                         "median_pct": round(100 * float(similar.median()), 2) if len(similar) else None,
                                         "p10_pct": round(100 * float(np.percentile(similar, 10)), 2) if len(similar) > 9 else None},
            "atr14_pct": ind.snapshot(d)["atr_pct"],
            "live_quote": ctx["quote"],
        })


class ResearchAnalyst(WebAnalyst):
    name = "Research Analyst"
    domain = ("Company-specific fundamentals and news flow: recent results and guidance, earnings date proximity, "
              "corporate actions (dividend, split, bonus, ex-dates), management/regulatory/legal news, promoter "
              "or block-deal activity, analyst rating changes, valuation vs peers. Flag any event inside the next "
              "10 sessions that could gap the stock.")
    search_task = ("Research this NSE-listed company: latest quarterly results, upcoming earnings/ex-dates, "
                   "material news in the last 2 weeks, and any red flags.")

    def evidence(self, ctx):
        return f"Last price: {ctx['quote']['ltp']}"


class MarketResearcher:
    """Whole-market view, runs once per scan (not per stock)."""
    name = "Market Researcher"

    def __init__(self, model: str):
        self.model = model

    def analyse(self, ctx: dict) -> MarketReport:
        sys = ("You are the market researcher of an Indian-equities swing desk. Assess the overall market regime "
               "for the next few sessions from the index data given plus live web research: global cues (US, Asia, "
               "crude, USD/INR), FII/DII flows, India VIX, sector rotation, and scheduled events (RBI, results "
               "season, expiry, budget, elections). Be concrete and dated.")
        notes = llm.web_research(model=self.model, system=sys, max_searches=6,
                                 user=f"Today: {ctx['today']}\nIndex/breadth data from Dhan:\n{_j(ctx['market'])}\n"
                                      "Search for the latest market wrap, FII/DII flows, India VIX and upcoming events.")
        return llm.structured(MarketReport, model=self.model, system=sys,
                              user=f"Research notes:\n{notes}\n\nData:\n{_j(ctx['market'])}\n\nGive your report.")


def stock_analysts(model: str) -> list[Analyst]:
    return [TechnicalAnalyst(model), ResearchAnalyst(model), TrendAnalyst(model), DataAnalyst(model)]
