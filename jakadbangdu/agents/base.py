"""Every analyst is an independent agent: own prompt, own data slice, no shared state."""
from .. import llm
from ..schemas import AnalystReport

COMMON = """You are one member of an Indian-equities (NSE) swing-trading desk. You work alone in your own domain and
report to the head trader, Jakadbangdu. Be evidence-led and concrete; cite numbers you were given. Never invent
data you were not given. If evidence is thin, say NEUTRAL with low conviction rather than forcing a view.
The desk trades long-only delivery positions held for days to a few weeks. Prices are in INR."""


class Analyst:
    name = "analyst"
    domain = ""

    def __init__(self, model: str):
        self.model = model

    def system(self) -> str:
        return f"{COMMON}\n\nYOUR DOMAIN: {self.name}.\n{self.domain}"

    def evidence(self, ctx: dict) -> str:  # subclasses build the evidence text
        raise NotImplementedError

    def analyse(self, ctx: dict) -> AnalystReport:
        rep = llm.structured(AnalystReport, model=self.model, system=self.system(),
                             user=f"Symbol: {ctx['symbol']}\n\n{self.evidence(ctx)}\n\nGive your report.")
        rep.analyst = self.name
        return rep


class WebAnalyst(Analyst):
    """Analysts that need live information from the web (news, filings, macro)."""
    search_task = ""

    def analyse(self, ctx: dict) -> AnalystReport:
        notes = llm.web_research(model=self.model, system=self.system(), max_searches=5,
                                 user=f"Symbol: {ctx['symbol']}\nToday: {ctx['today']}\n{self.search_task}\n\n"
                                      f"Context from the desk:\n{self.evidence(ctx)}\n\n"
                                      "Search, then write concise research notes with dates and sources.")
        rep = llm.structured(AnalystReport, model=self.model, system=self.system(),
                             user=f"Symbol: {ctx['symbol']}\n\nYour research notes:\n{notes}\n\nConvert to your report.")
        rep.analyst = self.name
        return rep
