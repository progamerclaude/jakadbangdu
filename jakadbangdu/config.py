"""Hard limits and settings. Rules the *user* fixed live here; everything else
(SL, target, sizing within the safety ceiling, holding period) is the bot's call."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(os.getenv("JAKAD_DATA_DIR", "data"))

NIFTY200_CACHE = DATA_DIR / "nifty200.csv"   # universe: NSE's official Nifty 200 list (see universe.py)


@dataclass(frozen=True)
class Settings:
    # --- User-mandated rules -------------------------------------------------
    starting_capital: float = 500_000.0   # INR 5 lakh
    min_free_capital_pct: float = 0.30    # at least 30% must stay free
    max_open_positions: int = 5

    # --- Safety ceilings (code-enforced backstops, not strategy) -------------
    max_risk_per_trade_pct: float = 0.02  # bot picks risk <= this % of capital
    min_reward_risk: float = 1.0          # refuse trades with RR below this
    min_target_pct: float = 0.10          # owner's rule: skip any trade whose target is < +10% above entry
    max_single_position_pct: float = 0.20 # one stock <= 20% of capital

    # --- Setup rules (owner-specified): stack Price>MA20>MA40>MA89>MA100, pullback to 20/40, SL below swing low ---
    ma_type: str = "ema"                  # "ema" or "sma"
    ma_periods: tuple = (20, 40, 89, 100)
    pullback_tol: float = 0.01            # low within 1% of the MA counts as a touch
    pivot_n: int = 3                      # swing = fractal with 3 bars each side
    sl_buffer_atr: float = 0.1            # SL sits this many ATR below the swing low
    max_stop_pct: float = 0.10            # skip if structural stop is wider than 10% of price
    chase_limit_pct: float = 0.015        # skip if live price is >1.5% above the signal bar's close
    max_adds: int = 2                     # pyramid adds per trade, each on a fresh break of structure
    add_fraction: float = 0.5             # each add <= 50% of the initial quantity
    # Optional market filter (NOT part of the owner's rules): when on, no new entries unless NIFTY close > its EMA50.
    # Off by default: stocks can trend independently of NIFTY, and the stock-level stack + RS ranking already select strength.
    require_market_uptrend: bool = field(default_factory=lambda: os.getenv("JAKAD_REGIME_FILTER", "").lower() in ("1", "true", "yes"))
    exit_at_target: bool = False          # trend riding: target is planning-only; exit via trailing SL/review

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
    # Dhan history requests are spaced this far apart (owner's choice: 20s, no hurry). Quotes keep their own 1/sec limit.
    data_request_interval_s: float = field(default_factory=lambda: float(os.getenv("JAKAD_DATA_INTERVAL_S", "20")))
    db_path: Path = field(default_factory=lambda: DATA_DIR / "journal.db")
    playbook_path: Path = field(default_factory=lambda: DATA_DIR / "playbook.md")
    dhan_client_id: str = field(default_factory=lambda: os.getenv("DHAN_CLIENT_ID", ""))
    dhan_access_token: str = field(default_factory=lambda: os.getenv("DHAN_ACCESS_TOKEN", ""))

    @property
    def live(self) -> bool:
        return self.mode == "live"
