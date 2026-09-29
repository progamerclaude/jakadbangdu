"""Deterministic risk gate. The LLM proposes; this code disposes.
Enforces the user's rules: 5-lakh book, >=30% free, <=5 open positions."""
from dataclasses import dataclass

from .config import Settings


@dataclass
class Sized:
    ok: bool
    reason: str = ""
    quantity: int = 0
    cost: float = 0.0
    risk_amount: float = 0.0


class Book:
    """Capital accounting on cost basis: capital = starting + realized P&L."""

    def __init__(self, s: Settings, journal):
        self.s, self.j = s, journal

    @property
    def capital(self) -> float:
        return self.s.starting_capital + self.j.realized_pnl()

    @property
    def deployed(self) -> float:
        return self.j.deployed_capital()

    @property
    def free(self) -> float:
        return self.capital - self.deployed

    @property
    def open_count(self) -> int:
        return len(self.j.open_trades())

    def summary(self) -> dict:
        return {"capital": round(self.capital), "deployed": round(self.deployed), "free": round(self.free),
                "free_pct": round(100 * self.free / self.capital, 1), "open_positions": self.open_count,
                "max_open_positions": self.s.max_open_positions,
                "min_free_pct": round(100 * self.s.min_free_capital_pct)}

    def size_trade(self, symbol: str, entry: float, sl: float, target: float, risk_pct: float) -> Sized:
        s = self.s
        if self.open_count >= s.max_open_positions:
            return Sized(False, f"already {self.open_count} open positions (max {s.max_open_positions})")
        if any(t["symbol"] == symbol for t in self.j.open_trades()):
            return Sized(False, f"{symbol} already open")
        if not (0 < sl < entry < target):
            return Sized(False, f"levels invalid for a long: need 0 < SL({sl}) < entry({entry}) < target({target})")
        rr = (target - entry) / (entry - sl)
        if rr < s.min_reward_risk:
            return Sized(False, f"reward:risk {rr:.2f} below floor {s.min_reward_risk}")
        risk_pct = max(0.0, min(risk_pct, s.max_risk_per_trade_pct))
        if risk_pct <= 0:
            return Sized(False, "risk_pct must be > 0")
        cap = self.capital
        qty = int(cap * risk_pct / (entry - sl))                      # risk-based size
        qty = min(qty, int(cap * s.max_single_position_pct / entry))  # concentration cap
        max_spend = self.free - cap * s.min_free_capital_pct          # keep 30% free after this trade
        qty = min(qty, int(max_spend / entry)) if max_spend > 0 else 0
        if qty < 1:
            return Sized(False, f"no room: free {self.free:,.0f}, must keep {cap * s.min_free_capital_pct:,.0f} free")
        return Sized(True, "ok", qty, qty * entry, qty * (entry - sl))
