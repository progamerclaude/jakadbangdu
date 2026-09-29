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

    def __init__(self, s: Settings, journal, strategy=None):
        from .performance import Performance
        from .strategy import Strategy
        self.s, self.j = s, journal
        self.perf = Performance(s, journal)
        self.strategy = strategy or Strategy(risk_pct_cap=s.max_risk_per_trade_pct, min_reward_risk=s.min_reward_risk,
                                             min_confidence=0)

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
                "min_free_pct": round(100 * self.s.min_free_capital_pct),
                "risk_cap_pct": round(100 * self.strategy.risk_pct_cap, 2), "month": self.perf.month(),
                "risk_multiplier": self.perf.risk_multiplier()[0]}

    def size_trade(self, symbol: str, entry: float, sl: float, target: float, risk_pct: float,
                   setup: str = "", confidence: int = 100) -> Sized:
        s, st = self.s, self.strategy
        mult, why = self.perf.risk_multiplier()
        if mult == 0:
            return Sized(False, why)
        if setup.strip().lower() in st.avoid_setups:
            return Sized(False, f"setup '{setup}' is on the avoid list (self-review)")
        if confidence < st.min_confidence:
            return Sized(False, f"confidence {confidence} below self-set minimum {st.min_confidence}")
        if self.open_count >= s.max_open_positions:
            return Sized(False, f"already {self.open_count} open positions (max {s.max_open_positions})")
        if any(t["symbol"] == symbol for t in self.j.open_trades()):
            return Sized(False, f"{symbol} already open")
        if not (0 < sl < entry < target):
            return Sized(False, f"levels invalid for a long: need 0 < SL({sl}) < entry({entry}) < target({target})")
        rr = (target - entry) / (entry - sl)
        if rr < max(s.min_reward_risk, st.min_reward_risk):
            return Sized(False, f"reward:risk {rr:.2f} below floor {max(s.min_reward_risk, st.min_reward_risk)}")
        risk_pct = max(0.0, min(risk_pct, s.max_risk_per_trade_pct, st.risk_pct_cap)) * mult
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

    def size_add(self, t, price: float, new_sl: float) -> Sized:
        """Pyramid add on BoS. Total risk after the add (at the trailed stop) may not exceed the ORIGINAL 1R."""
        s = self.s
        mult, why = self.perf.risk_multiplier()
        if mult == 0:
            return Sized(False, why)
        if t["adds"] >= s.max_adds:
            return Sized(False, f"max adds ({s.max_adds}) reached")
        if not (0 < new_sl < price):
            return Sized(False, "stop must be below price")
        existing = max(0.0, t["entry_price"] - new_sl) * t["quantity"]   # 0 once the stop is above average entry
        budget = t["risk_amount"] * mult - existing
        if budget <= 0:
            return Sized(False, "no risk budget left for an add")
        cap = self.capital
        qty = min(int(budget / (price - new_sl)), int(t["quantity"] * s.add_fraction),
                  int(cap * s.max_single_position_pct / price) - t["quantity"])
        max_spend = self.free - cap * s.min_free_capital_pct
        qty = min(qty, int(max_spend / price)) if max_spend > 0 else 0
        if qty < 1:
            return Sized(False, "no room to add (risk budget, concentration or free-capital floor)")
        return Sized(True, "ok", qty, qty * price, qty * (price - new_sl))
