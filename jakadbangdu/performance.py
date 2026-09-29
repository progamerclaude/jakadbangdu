"""Objective tracking: progress vs the monthly target, and evidence for self-correction."""
import json
from datetime import datetime

from .config import Settings


def _stats(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}
    pnl = [t["pnl"] for t in trades]
    wins, losses = [p for p in pnl if p > 0], [p for p in pnl if p <= 0]
    rs = [t["r_multiple"] for t in trades if t["r_multiple"] is not None]
    eq, peak, mdd = 0.0, 0.0, 0.0
    for p in pnl:
        eq += p
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return {"trades": len(trades), "win_rate": round(len(wins) / len(trades), 2), "total_pnl": round(sum(pnl)),
            "avg_win": round(sum(wins) / len(wins)) if wins else 0, "avg_loss": round(sum(losses) / len(losses)) if losses else 0,
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) else None,
            "expectancy_r": round(sum(rs) / len(rs), 2) if rs else None, "max_drawdown": round(mdd)}


class Performance:
    def __init__(self, s: Settings, journal):
        self.s, self.j = s, journal

    def month(self, now: datetime | None = None) -> dict:
        now = now or datetime.now()
        m = now.strftime("%Y-%m")
        base = self.s.starting_capital + self.j.realized_before(m)     # capital at start of month
        pnl = self.j.realized_between(m)
        target = base * self.s.monthly_target_pct
        days_in_month = 30
        elapsed = min(now.day / days_in_month, 1.0)
        return {"month": m, "start_capital": round(base), "realized_pnl": round(pnl), "pnl_pct": round(100 * pnl / base, 2),
                "target_pnl": round(target), "target_pct": round(100 * self.s.monthly_target_pct, 1),
                "progress_of_target_pct": round(100 * pnl / target, 1) if target else 0,
                "expected_progress_pct": round(100 * elapsed, 1),
                "status": "AHEAD" if pnl / target > elapsed else "BEHIND"}

    def risk_multiplier(self, now: datetime | None = None) -> tuple[float, str]:
        """Circuit breakers. Falling behind target NEVER increases risk; drawdown reduces it."""
        m = self.month(now)
        p = m["pnl_pct"] / 100
        if p <= -self.s.dd_halt_pct:
            return 0.0, f"month P&L {m['pnl_pct']}% <= -{100*self.s.dd_halt_pct:.0f}%: trading halted for the month"
        if p <= -self.s.dd_halve_risk_pct:
            return 0.5, f"month P&L {m['pnl_pct']}%: risk halved"
        if self.s.lock_in_after_target and p >= self.s.monthly_target_pct:
            return 0.5, f"monthly target reached ({m['pnl_pct']}%): risk halved to protect the month"
        return 1.0, "normal"

    def report(self) -> dict:
        closed = self.j.all_closed()
        reviews = [json.loads(t["review"]) for t in closed if t["review"]]
        mistakes: dict[str, int] = {}
        for r in reviews:
            if r["mistake_type"] != "none":
                mistakes[r["mistake_type"]] = mistakes.get(r["mistake_type"], 0) + 1
        applied = [t for t in closed if t["decision"] and json.loads(t["decision"]).get("lessons_applied")]
        not_applied = [t for t in closed if t not in applied]
        return {"month": self.month(), "all_time": _stats(closed), "last_10": _stats(closed[-10:]),
                "by_setup": self.j.stats_by_setup(), "analyst_scorecard": self.j.analyst_scorecard(),
                "recurring_mistakes": dict(sorted(mistakes.items(), key=lambda kv: -kv[1])),
                "lesson_effect": {"with_lessons_applied": _stats(applied), "without": _stats(not_applied)}}
