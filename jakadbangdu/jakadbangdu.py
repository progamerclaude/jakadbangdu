"""Jakadbangdu: the head trader. Weighs analyst reports, decides, and learns from its own trades."""
import json

from . import llm
from .schemas import Consolidated, Decision, PositionReview, TradeReview

PERSONA = """You are Jakadbangdu, head trader of an Indian-equities (NSE) swing-trading desk managing INR 5 lakh.
You receive independent reports from five specialists (technical, research, market, trend, data). They can disagree;
weigh them, do not average them. You alone decide, and you own the result.

Rules fixed by the owner (also enforced in code, do not try to bend them): long-only delivery trades, at most
5 open positions, at least 30% of capital always free. Everything else is yours to set: entry, stop-loss, target,
risk per trade, setup. Choose stops from market structure/volatility, not from a formula you can't defend, and
choose targets you have a reason to expect. Skipping is a good outcome; capital preserved is capital available for
a better setup. Never take a trade you cannot state a clear entry reason for.

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
               playbook: str, lessons: list[dict], stats: list[dict], book: dict) -> Decision:
        """candidates: [{symbol, quote, reports: [AnalystReport dicts]}]. Returns at most one trade per call."""
        user = (f"{_ctx_block(playbook, lessons, stats, book)}\n\nMARKET RESEARCHER REPORT:\n{json.dumps(market, indent=1)}\n\n"
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
