from typing import Literal

from pydantic import BaseModel, Field

Stance = Literal["BULLISH", "NEUTRAL", "BEARISH"]


class AnalystReport(BaseModel):
    """What every analyst returns to Jakadbangdu (same shape, independent domains)."""
    analyst: str = ""
    stance: Stance
    conviction: int = Field(description="0-100, how strong the evidence is")
    summary: str = Field(description="2-4 sentences, evidence first")
    key_points: list[str]
    risks: list[str]
    suggested_entry: float | None = Field(default=None, description="Only if you have a concrete level")
    suggested_stop: float | None = None
    suggested_target: float | None = None
    setup: str = Field(default="", description="Named setup if one applies, e.g. 'pullback-to-ema21', 'breakout'")


class MarketReport(BaseModel):
    regime: Literal["RISK_ON", "MIXED", "RISK_OFF"]
    conviction: int
    summary: str
    key_points: list[str]
    risks: list[str]
    sector_leaders: list[str]
    sector_laggards: list[str]
    event_risks: list[str] = Field(description="Scheduled events in the next few sessions")


class Decision(BaseModel):
    action: Literal["TRADE", "SKIP"]
    symbol: str = ""
    setup: str = Field(default="", description="Short reusable setup name; be consistent so stats aggregate")
    entry_price: float = 0
    stop_loss: float = 0
    target: float = 0
    risk_pct: float = Field(default=0, description="Fraction of capital risked, e.g. 0.01 for 1%")
    confidence: int = 0
    entry_reason: str = ""
    lessons_applied: list[str] = Field(default_factory=list, description="Which past lessons changed this decision")
    skip_reason: str = ""


class PositionReview(BaseModel):
    action: Literal["HOLD", "EXIT", "TRAIL"]
    new_stop_loss: float = 0
    new_target: float = 0
    reason: str


class TradeReview(BaseModel):
    """Post-mortem written after every closed trade."""
    outcome_quality: Literal["GOOD_PROCESS_GOOD_RESULT", "GOOD_PROCESS_BAD_RESULT",
                             "BAD_PROCESS_GOOD_RESULT", "BAD_PROCESS_BAD_RESULT"]
    mistake_type: str = Field(description="One of: none, chased-entry, stop-too-tight, stop-too-wide, target-too-greedy, "
                                          "ignored-market-regime, ignored-analyst-warning, poor-setup, oversized, "
                                          "late-exit, early-exit, news-shock, other")
    what_went_wrong: str
    what_went_right: str
    rule_for_future: str = Field(description="One concrete, testable rule. Empty string if nothing to learn.")
    blame_analyst: str = Field(description="Which analyst's input misled the decision, or 'none'")


class Consolidated(BaseModel):
    playbook_markdown: str = Field(description="Rewritten playbook: numbered rules, grouped by theme, deduplicated")
    retire_lesson_ids: list[int] = Field(description="Lesson ids now fully covered by the playbook or disproved")


class StrategyUpdate(BaseModel):
    """Periodic self-correction. Numeric fields are ENFORCED in code (within hard bounds)."""
    assessment: str = Field(description="Honest read of performance and why, evidence from the numbers given")
    recurring_mistakes: list[str]
    avoid_setups: list[str] = Field(description="Setup names to stop trading (only with evidence: enough trades, negative expectancy)")
    favor_setups: list[str]
    risk_pct_cap: float = Field(description="Max fraction of capital risked per trade going forward, e.g. 0.01. "
                                            "Lower it after losses/poor expectancy; only raise it with strong positive evidence")
    min_reward_risk: float = Field(description="Minimum reward:risk to accept a trade")
    min_confidence: int = Field(description="Minimum confidence (0-100) to accept a trade")
    analyst_guidance: list[str] = Field(description="How to weigh each analyst given their scorecard")
    changes_and_rationale: str
