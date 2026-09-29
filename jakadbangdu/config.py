"""Hard limits and settings. Rules the *user* fixed live here; everything else
(SL, target, sizing within the safety ceiling, holding period) is the bot's call."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(os.getenv("JAKAD_DATA_DIR", "data"))

# Default scan universe (NSE cash symbols). Edit or pass --symbols.
DEFAULT_WATCHLIST = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "SBIN", "BHARTIARTL", "ITC",
    "LT", "AXISBANK", "KOTAKBANK", "HINDUNILVR", "MARUTI", "SUNPHARMA", "TATAMOTORS",
    "TITAN", "BAJFINANCE", "ASIANPAINT", "NTPC", "POWERGRID", "ONGC", "ADANIPORTS",
    "M&M", "ULTRACEMCO", "WIPRO", "HCLTECH", "TATASTEEL", "JSWSTEEL", "COALINDIA", "DRREDDY",
]


@dataclass(frozen=True)
class Settings:
    # --- User-mandated rules -------------------------------------------------
    starting_capital: float = 500_000.0   # INR 5 lakh
    min_free_capital_pct: float = 0.30    # at least 30% must stay free
    max_open_positions: int = 5

    # --- Safety ceilings (code-enforced backstops, not strategy) -------------
    max_risk_per_trade_pct: float = 0.02  # bot picks risk <= this % of capital
    min_reward_risk: float = 1.0          # refuse trades with RR below this
    max_single_position_pct: float = 0.20 # one stock <= 20% of capital

    # --- Mandate & circuit breakers (bounds on the bot's self-adjustments) ---
    monthly_target_pct: float = 0.10      # objective: 10% of capital per month
    dd_halve_risk_pct: float = 0.04       # month P&L <= -4%  -> risk halved
    dd_halt_pct: float = 0.08             # month P&L <= -8%  -> no new entries this month
    lock_in_after_target: bool = True     # once the month's target is hit, risk halved (never chase past it)
    review_every_n_trades: int = 5        # strategy review cadence (also weekly in `run`)
    strategy_path: Path = field(default_factory=lambda: DATA_DIR / "strategy.json")

    # --- Runtime -------------------------------------------------------------
    mode: str = field(default_factory=lambda: os.getenv("TRADING_MODE", "paper").lower())
    jakad_model: str = field(default_factory=lambda: os.getenv("JAKAD_MODEL", "claude-opus-5-5"))
    analyst_model: str = field(default_factory=lambda: os.getenv("ANALYST_MODEL", "claude-opus-5-5"))
    shortlist_size: int = 6               # candidates sent to the analyst panel per scan
    monitor_interval_s: int = 60
    db_path: Path = field(default_factory=lambda: DATA_DIR / "journal.db")
    playbook_path: Path = field(default_factory=lambda: DATA_DIR / "playbook.md")
    dhan_client_id: str = field(default_factory=lambda: os.getenv("DHAN_CLIENT_ID", ""))
    dhan_access_token: str = field(default_factory=lambda: os.getenv("DHAN_ACCESS_TOKEN", ""))

    @property
    def live(self) -> bool:
        return self.mode == "live"
