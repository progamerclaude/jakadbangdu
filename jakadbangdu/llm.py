"""Thin Claude wrapper: structured output via messages.parse, optional web search."""
_client = None


def client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


class Refused(RuntimeError):
    pass


def structured(schema, *, model: str, system: str, user: str, max_tokens: int = 16000):
    r = client().messages.parse(model=model, max_tokens=max_tokens, system=system,
                                messages=[{"role": "user", "content": user}], output_format=schema)
    if r.stop_reason == "refusal":
        raise Refused("model refused")
    if r.parsed_output is None:
        raise RuntimeError(f"no parsed output (stop_reason={r.stop_reason})")
    return r.parsed_output


def web_research(*, model: str, system: str, user: str, max_searches: int = 5) -> str:
    """Run a search-enabled turn and return the final text (server-side web_search)."""
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}]
    messages = [{"role": "user", "content": user}]
    for _ in range(6):  # server tools may pause a long turn; resume it
        r = client().messages.create(model=model, max_tokens=16000, system=system, tools=tools, messages=messages)
        if r.stop_reason == "refusal":
            raise Refused("model refused")
        if r.stop_reason != "pause_turn":
            return "\n".join(b.text for b in r.content if b.type == "text")
        messages = messages + [{"role": "assistant", "content": r.content}]
    raise RuntimeError("web research did not finish")
