"""Rules-only mode: the same setup, journal, risk gate and trailing/BoS management, with NO LLM and no API key.
Drop-in replacement for the head trader: takes the best-ranked valid signal mechanically."""
from .schemas import Consolidated, Decision, MarketReport, PositionReview, StrategyUpdate, TradeReview


class NullMarket:
    """No market researcher in rules-only mode (the NIFTY>EMA50 regime filter in code still applies)."""

    def analyse(self, ctx):
        return MarketReport(regime="MIXED", conviction=0, summary="rules-only mode", key_points=[], risks=[],
                            sector_leaders=[], sector_laggards=[], event_risks=[])


class RulesTrader:
    """Same interface as Jakadbangdu; every method is deterministic."""

    def decide(self, *, candidates, strategy=None, **kw) -> Decision:
        if not candidates:
            return Decision(action="SKIP", skip_reason="no candidates")
        c = candidates[0]                       # already ranked by 60d relative strength vs NIFTY
        sg, e = c["setup_signal"], c["quote"]["ltp"]
        risk = e - sg["sl"]
        target = max(sg["swing_high"], e + 2 * risk)          # at least 2R; swing high if further
        return Decision(
            action="TRADE", symbol=c["symbol"], setup=sg["kind"], entry_price=e, stop_loss=sg["sl"], target=round(target, 2),
            risk_pct=(strategy or {}).get("risk_pct_cap", 0.01), confidence=100,
            entry_reason=(f"{sg['kind']}: MA20>40>89>100 stack, pullback to the MA, SL below swing low {sg['swing_low']}, "
                          f"RS60 {sg['rs60']}% vs NIFTY, ADX {sg['adx']}"))

    def review_position(self, **kw) -> PositionReview:
        return PositionReview(action="HOLD", reason="rules-only: exits are the trailing stop")

    def reflect(self, trade: dict) -> TradeReview:
        """Mechanical post-mortem from numbers only (no lessons are invented)."""
        r = trade.get("r_multiple")
        win = (trade.get("pnl") or 0) > 0
        return TradeReview(
            outcome_quality="GOOD_PROCESS_GOOD_RESULT" if win else "GOOD_PROCESS_BAD_RESULT",
            mistake_type="none", what_went_wrong="" if win else f"stopped out at {r}R ({trade.get('exit_reason')})",
            what_went_right=f"closed at {r}R" if win else "", rule_for_future="", blame_analyst="none")

    def consolidate(self, playbook, lessons, stats) -> Consolidated:
        return Consolidated(playbook_markdown=playbook, retire_lesson_ids=[])

    def strategy_review(self, *, current, **kw) -> StrategyUpdate:
        return StrategyUpdate(assessment="rules-only: parameters fixed", recurring_mistakes=[], avoid_setups=current["avoid_setups"],
                              favor_setups=[], risk_pct_cap=current["risk_pct_cap"], min_reward_risk=current["min_reward_risk"],
                              min_confidence=current["min_confidence"], analyst_guidance=[], changes_and_rationale="none")
