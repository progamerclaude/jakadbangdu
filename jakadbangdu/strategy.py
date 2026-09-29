"""Self-tuned parameters. The bot proposes; values are clamped to the owner's hard bounds in config."""
import json
from dataclasses import asdict, dataclass, field

from .config import Settings


@dataclass
class Strategy:
    risk_pct_cap: float = 0.01
    min_reward_risk: float = 1.5
    min_confidence: int = 60
    avoid_setups: list[str] = field(default_factory=list)
    favor_setups: list[str] = field(default_factory=list)
    analyst_guidance: list[str] = field(default_factory=list)
    assessment: str = ""


def load(s: Settings) -> Strategy:
    if s.strategy_path.exists():
        return Strategy(**json.loads(s.strategy_path.read_text()))
    return Strategy(risk_pct_cap=min(0.01, s.max_risk_per_trade_pct), min_reward_risk=max(1.5, s.min_reward_risk))


def apply_update(s: Settings, cur: Strategy, u) -> Strategy:
    """Clamp: risk can never exceed the hard ceiling, RR never below the hard floor; risk may rise at most 25% per review."""
    new = Strategy(
        risk_pct_cap=round(max(0.0025, min(u.risk_pct_cap, s.max_risk_per_trade_pct, cur.risk_pct_cap * 1.25)), 4),
        min_reward_risk=max(u.min_reward_risk, s.min_reward_risk),
        min_confidence=int(max(0, min(u.min_confidence, 90))),
        avoid_setups=sorted({x.strip().lower() for x in u.avoid_setups if x.strip()}),
        favor_setups=[x.strip().lower() for x in u.favor_setups],
        analyst_guidance=u.analyst_guidance, assessment=u.assessment)
    s.strategy_path.parent.mkdir(parents=True, exist_ok=True)
    s.strategy_path.write_text(json.dumps(asdict(new), indent=1))
    return new
