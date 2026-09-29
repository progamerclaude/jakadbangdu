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
