"""Jakadbangdu: the head trader. Weighs analyst reports, decides, and learns from its own trades."""
import json

from . import llm
from .schemas import Consolidated, Decision, PositionReview, StrategyUpdate, TradeReview

PERSONA = """You are Jakadbangdu, head trader of an Indian-equities (NSE) swing-trading desk managing INR 5 lakh.
You receive independent reports from five specialists (technical, research, market, trend, data). They can disagree;
weigh them, do not average them. You alone decide, and you own the result.

Rules fixed by the owner (also enforced in code, do not try to bend them): long-only delivery trades, at most
5 open positions, at least 30% of capital always free. Everything else is yours to set: entry, stop-loss, target,
risk per trade, setup. Choose stops from market structure/volatility, not from a formula you can't defend, and
choose targets you have a reason to expect. Skipping is a good outcome; capital preserved is capital available for
a better setup. Never take a trade you cannot state a clear entry reason for.

MANDATE: grow the book by 10% of capital per month. That is the objective you are measured on, but it is an
outcome of good process, not something to force. Being BEHIND pace is never a reason to take marginal trades, widen
risk or lower standards; the code will cut your risk after drawdowns and after the target is hit. The way to reach
10% is few high-quality setups, well-placed stops, realistic targets and repeating what your own data shows works.

THE DESK'S SETUP (fixed by the owner, applied in code before you see anything): trend stack MA20>MA40>MA89>MA100,
entry on a pullback to the 20 or 40, stop below the last swing low, adds on break of structure with a trailing
stop. Every candidate carries a `setup_signal`: its entry, stop, swing high, stop distance % and reward:risk to the
swing high are FIXED; you cannot move them. Your job is quality control on that signal: TRADE or SKIP it, choose
risk_pct, confidence, and a target you can defend (this is a trend-riding strategy: the trailing stop does the exit,
the target is your planning level for the reward:risk gate). Skip when the signal is technically valid but the
context is not: event risk inside a few sessions (results, ex-date), weak market, a broken-looking chart, or a
setup kind/condition your own playbook shows losing money.

You learn from mistakes. Below you get your PLAYBOOK (rules distilled from past trades), recent LESSONS, and your
statistics by setup. Actively apply them: if a lesson applies, follow it or say why not, and list it under
lessons_applied. Use consistent, short setup names so statistics aggregate."""


def _ctx_block(playbook: str, lessons: list[dict], stats: list[dict], book: dict) -> str:
    ls = "\n".join(f"- [{l['mistake_type']}] {l['rule']}" for l in lessons) or "(none yet)"
    return (f"PLAYBOOK:\n{playbook or '(empty: no closed trades yet)'}\n\nRECENT LESSONS:\n{ls}\n\n"
            f"STATS BY SETUP: {json.dumps(stats)}\n\nBOOK: {json.dumps(book)}")


class Jakadbangdu:
    def __init__(self, model: str):
        self.model = model

    def decide(self, *, candidates: list[dict], market: dict, open_positions: list[dict],
               playbook: str, lessons: list[dict], stats: list[dict], book: dict,
               scorecard: list[dict] | None = None, strategy: dict | None = None) -> Decision:
        """candidates: [{symbol, quote, reports: [AnalystReport dicts]}]. Returns at most one trade per call."""
        user = (f"{_ctx_block(playbook, lessons, stats, book)}\n\nYOUR SELF-SET STRATEGY LIMITS (enforced in code): "
                f"{json.dumps(strategy)}\nANALYST TRACK RECORD (hit rate on closed trades; weigh accordingly): "
                f"{json.dumps(scorecard)}\n\nMARKET RESEARCHER REPORT:\n{json.dumps(market, indent=1)}\n\n"
                f"OPEN POSITIONS: {json.dumps(open_positions)}\n\nCANDIDATES:\n{json.dumps(candidates, indent=1, default=str)}\n\n"
                "Pick the single best trade among the candidates, or SKIP. For a TRADE give a realistic entry near the "
                "live price, stop_loss, target, risk_pct (fraction of capital you risk, max 0.02), and a specific "
                "entry_reason that names the evidence. For SKIP give skip_reason.")
        return llm.structured(Decision, model=self.model, system=PERSONA, user=user)

    def review_position(self, *, trade: dict, quote: dict, technical: dict, playbook: str, lessons: list[dict]) -> PositionReview:
        user = (f"PLAYBOOK:\n{playbook or '(empty)'}\n\nLESSONS: {json.dumps([l['rule'] for l in lessons])}\n\n"
                f"OPEN TRADE: {json.dumps(trade, default=str)}\nLIVE QUOTE: {json.dumps(quote)}\n"
                f"CURRENT INDICATORS: {json.dumps(technical)}\n\n"
                "Decide HOLD, EXIT now, or TRAIL (raise the stop and/or move the target; a stop may only move up). "
                "Do not exit merely from discomfort: exit when the reason you entered is broken.")
        return llm.structured(PositionReview, model=self.model, system=PERSONA, user=user)

    def reflect(self, trade: dict) -> TradeReview:
        """Post-mortem on a closed trade, using the reports and decision as they were at entry."""
        user = (f"You closed a trade. Do an honest post-mortem separating decision quality from luck.\n\n"
                f"TRADE: {json.dumps({k: trade[k] for k in trade if k not in ('analyst_reports','decision','review')}, default=str)}\n\n"
                f"ANALYST REPORTS AT ENTRY: {trade['analyst_reports']}\n\nYOUR DECISION AT ENTRY: {trade['decision']}\n\n"
                "Compare entry/SL/target with what price did (see exit). Was the stop placed sensibly? Was the target "
                "realistic? Did you ignore a warning that was in the reports? End with ONE concrete, testable rule for "
                "future trades (empty string if there is genuinely nothing to learn).")
        return llm.structured(TradeReview, model=self.model, system=PERSONA, user=user)

    def consolidate(self, playbook: str, lessons: list[dict], stats: list[dict]) -> Consolidated:
        user = (f"CURRENT PLAYBOOK:\n{playbook or '(empty)'}\n\nLESSONS (id: rule):\n"
                + "\n".join(f"{l['id']}: [{l['mistake_type']}] {l['rule']} (why: {l['what_went_wrong']})" for l in lessons)
                + f"\n\nSTATS BY SETUP: {json.dumps(stats)}\n\n"
                "Rewrite the playbook: merge duplicates, drop rules the stats contradict, keep it under ~25 numbered "
                "rules, each specific and testable. List lesson ids now fully absorbed into the playbook.")
        return llm.structured(Consolidated, model=self.model, system=PERSONA, user=user)

    def strategy_review(self, *, report: dict, current: dict, playbook: str, lessons: list[dict]) -> StrategyUpdate:
        """Periodic self-audit: turn measured results into enforced parameter changes."""
        user = (f"PERFORMANCE REPORT:\n{json.dumps(report, indent=1)}\n\nCURRENT STRATEGY PARAMETERS: {json.dumps(current)}\n\n"
                f"PLAYBOOK:\n{playbook or '(empty)'}\n\nOPEN LESSONS: {json.dumps([l['rule'] for l in lessons])}\n\n"
                "You are auditing yourself against the 10%/month mandate. Diagnose WHY you are ahead/behind using the "
                "numbers: win rate vs reward:risk, which setups make/lose money, which analysts mislead you, which mistake "
                "types recur, whether applying lessons actually improved results. Small samples are noise: do not blacklist "
                "a setup on fewer than ~5 trades, and do not raise risk without strong, sustained positive expectancy. "
                "Being behind target must NOT raise risk. Output new parameters (they are enforced) and say what you changed and why.")
        return llm.structured(StrategyUpdate, model=self.model, system=PERSONA, user=user)
