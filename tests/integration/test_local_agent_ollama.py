"""Live LocalAgent ↔ Ollama integration tests.

Skipped unless `OLLAMA_INTEGRATION=1`. See `tests/integration/conftest.py`
for env-var configuration. These tests are slow (each scenario is a real
LLM round trip — typically 2–15 seconds, multi-turn scenarios up to a
minute) so they're isolated into a separate `integration` subtree, never
run by the default `pytest` invocation.

Test taxonomy:
- *Smoke* (`test_plain_text_reply`, `test_history_persisted_to_disk`):
  basic plumbing.
- *Memory* (`test_multi_turn_recall`, `test_multi_fact_synthesis`):
  history actually reaches the model on subsequent turns.
- *Tool execution* (`test_tool_use_returns_real_year`,
  `test_secret_token_tool_was_invoked`): the model not only sees tools
  but actually invokes them and the results round-trip back. Verified
  with payloads the model could not produce by hallucination.
- *Planning* (`test_plan_respects_remembered_constraints`,
  `test_plan_with_tool_and_memory`): multi-turn planning where prior
  constraints + tool output must combine in the final answer.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import AsyncIterator

import pytest
from pydantic import BaseModel

from sampyclaw.agents import (
    AgentContext,
    FunctionTool,
    LocalAgent,
    ToolRegistry,
    default_tools,
)
from sampyclaw.agents.history import ConversationHistory
from sampyclaw.config.paths import SampyclawPaths
from sampyclaw.plugin_sdk.channel_contract import ChannelTarget, InboundEnvelope


def _envelope(text: str) -> InboundEnvelope:
    return InboundEnvelope(
        channel="telegram",
        account_id="main",
        target=ChannelTarget(channel="telegram", account_id="main", chat_id="42"),
        sender_id="integration",
        text=text,
        received_at=time.time(),
    )


async def _collect(agent: LocalAgent, env: InboundEnvelope, ctx: AgentContext) -> str:
    parts: list[str] = []
    async for sp in agent.handle(env, ctx):
        parts.append(sp.text or "")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Custom test-only tools that produce verifiable, hallucination-proof output.
# ---------------------------------------------------------------------------


class _NoArgs(BaseModel):
    model_config = {"extra": "forbid"}


def _secret_token_tool(secret: str) -> FunctionTool:
    """Build a tool whose only side effect is returning `secret`.

    If the secret appears in the assistant's reply, the model *must* have
    invoked the tool — there's no other channel for it to learn the value.
    """

    async def _handler(_: _NoArgs) -> str:
        return secret

    return FunctionTool(
        name="get_session_token",
        description=(
            "Return the user's session token. The session token is a short "
            "opaque string the user needs in order to log in. Always call "
            "this tool when the user asks for their session token."
        ),
        input_model=_NoArgs,
        handler=_handler,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_agent(
    *,
    paths: SampyclawPaths,
    base_url: str,
    model: str,
    tools: ToolRegistry,
    agent_id: str = "integration-local",
) -> LocalAgent:
    return LocalAgent(
        agent_id=agent_id,
        model=model,
        base_url=base_url,
        tools=tools,
        paths=paths,
        timeout=180.0,
    )


@pytest.fixture()
async def agent(tmp_path, ollama_base_url: str, ollama_model: str) -> AsyncIterator[LocalAgent]:  # type: ignore[no-untyped-def]
    paths = SampyclawPaths(home=tmp_path)
    paths.ensure_home()

    tools = ToolRegistry()
    tools.register_all(default_tools())

    agent = _build_agent(paths=paths, base_url=ollama_base_url, model=ollama_model, tools=tools)
    try:
        yield agent
    finally:
        await agent.aclose()


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------


async def test_plain_text_reply(agent: LocalAgent) -> None:
    """Model returns something non-empty for a simple prompt."""
    ctx = AgentContext(agent_id=agent.id, session_key="plain")
    out = await _collect(agent, _envelope("Reply with exactly the word OK."), ctx)
    assert out.strip(), "expected non-empty plain-text reply"


async def test_history_persisted_to_disk(agent: LocalAgent) -> None:
    """After a two-turn exchange the session file holds system + 2 user + 2 assistant."""
    ctx = AgentContext(agent_id=agent.id, session_key="persist")
    await _collect(agent, _envelope("Hi."), ctx)
    await _collect(agent, _envelope("Bye."), ctx)
    hist = ConversationHistory(agent._paths.session_file(agent.id, "persist"))
    assert len(hist) >= 5, f"history too short: {len(hist)}"
    assert hist.messages()[0]["role"] == "system"


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


async def test_multi_turn_recall(agent: LocalAgent) -> None:
    """Two-turn fact recall."""
    ctx = AgentContext(agent_id=agent.id, session_key="recall")
    await _collect(agent, _envelope("My favourite colour is teal. Remember that."), ctx)
    out = await _collect(agent, _envelope("What did I say my favourite colour was?"), ctx)
    assert "teal" in out.lower(), f"model forgot 'teal'; got: {out!r}"


async def test_multi_fact_synthesis(agent: LocalAgent) -> None:
    """Three facts arrive on three separate turns; the fourth turn must combine all three.

    Stresses that the entire history reaches the model — not just the last
    user message.
    """
    ctx = AgentContext(agent_id=agent.id, session_key="multi-fact")
    await _collect(agent, _envelope("Remember: my name is Sam."), ctx)
    await _collect(agent, _envelope("Remember: I live in Seoul."), ctx)
    await _collect(agent, _envelope("Remember: my pet's name is Mochi."), ctx)
    out = await _collect(
        agent,
        _envelope(
            "Summarise everything you know about me in one sentence including "
            "my name, my city, and my pet's name."
        ),
        ctx,
    )
    lowered = out.lower()
    missing = [k for k in ("sam", "seoul", "mochi") if k not in lowered]
    assert not missing, f"summary missing {missing}; got: {out!r}"


# ---------------------------------------------------------------------------
# Tool execution (verifiable: model could not invent the answer)
# ---------------------------------------------------------------------------


async def test_tool_use_returns_real_year(agent: LocalAgent) -> None:
    """`get_time` returns a real ISO timestamp; the year 2026 only appears
    in the reply if the tool was actually invoked.

    A model that ignored the tool would either refuse, hallucinate its
    training-cutoff year, or omit the year — none of which contain '2026'.
    """
    ctx = AgentContext(agent_id=agent.id, session_key="tool-real-year")
    out = await _collect(
        agent,
        _envelope(
            "Use the get_time tool to look up the current UTC time, then "
            "tell me what year it is. Include the four-digit year in your reply."
        ),
        ctx,
    )
    assert "2026" in out, f"expected '2026' (only available via tool) in reply; got: {out!r}"


async def test_secret_token_tool_was_invoked(
    tmp_path, ollama_base_url: str, ollama_model: str
) -> None:  # type: ignore[no-untyped-def]
    """Custom tool returns a per-test UUID. If the UUID appears in the
    assistant text, the model invoked the tool — there's no other channel
    for that value to leak."""
    secret = f"SAMPY-{uuid.uuid4().hex[:10].upper()}"

    paths = SampyclawPaths(home=tmp_path)
    paths.ensure_home()
    tools = ToolRegistry()
    tools.register(_secret_token_tool(secret))

    agent = _build_agent(paths=paths, base_url=ollama_base_url, model=ollama_model, tools=tools)
    try:
        ctx = AgentContext(agent_id=agent.id, session_key="secret")
        out = await _collect(
            agent,
            _envelope(
                "Please retrieve my session token and tell me what it is. "
                "You have a tool called get_session_token for this. "
                "Include the exact token verbatim in your reply."
            ),
            ctx,
        )
        assert secret in out, f"secret {secret!r} not in reply — tool was not invoked. Got: {out!r}"
    finally:
        await agent.aclose()


# ---------------------------------------------------------------------------
# Planning — memory + reasoning combined
# ---------------------------------------------------------------------------


async def test_plan_respects_remembered_constraints(agent: LocalAgent) -> None:
    """Constraints accumulate across three turns; the plan must respect all of them.

    Concretely: vegetarian + lactose-intolerant means a valid breakfast plan
    must avoid meat AND dairy. We verify the plan does not mention any
    forbidden ingredient, AND that it mentions food (so we know it actually
    produced a plan rather than refusing).
    """
    ctx = AgentContext(agent_id=agent.id, session_key="plan-constraints")
    await _collect(
        agent,
        _envelope("Remember: I am strictly vegetarian — I never eat meat or fish."),
        ctx,
    )
    await _collect(
        agent,
        _envelope(
            "Remember: I am also lactose intolerant — no milk, cheese, butter, yogurt, or cream."
        ),
        ctx,
    )
    out = await _collect(
        agent,
        _envelope(
            "Now plan me a simple breakfast I can make at home. Just list "
            "the items and a one-line reason."
        ),
        ctx,
    )
    lowered = out.lower()

    # Strip plant-based variants of dairy items before scanning, so
    # "almond milk" / "oat milk" / "plant milk" don't trip the dairy check —
    # those *honour* the lactose-intolerance constraint, they don't violate it.
    plant_prefix = (
        r"(plant[- ]based|plant|almond|soy|soya|oat|coconut|cashew|rice|hemp|"
        r"nut|nondairy|non[- ]dairy|dairy[- ]free|vegan)"
    )
    sanitized = re.sub(
        rf"\b{plant_prefix}[ -]+(milk|cheese|butter|yogurt|yoghurt|cream)\b",
        " ",
        lowered,
    )

    forbidden_patterns = [
        # meats
        r"\bbeef\b",
        r"\bpork\b",
        r"\bchicken\b",
        r"\bham\b",
        r"\bbacon\b",
        r"\bsausage\b",
        r"\bfish\b",
        r"\bsalmon\b",
        r"\btuna\b",
        r"\bshrimp\b",
        # dairy — \b on `cream` so "creamy" (an adjective often used for plant
        # alternatives) doesn't match.
        r"\bmilk\b",
        r"\bcheese\b",
        r"\bbutter\b",
        r"\byogurt\b",
        r"\byoghurt\b",
        r"\bcream\b",
    ]
    hits = [p for p in forbidden_patterns if re.search(p, sanitized)]
    assert not hits, (
        f"plan violates remembered constraints (mentions {hits}); "
        f"sanitized={sanitized!r}; got: {out!r}"
    )
    # Plan must actually reference some food. Cheap shape check.
    food_signals = [
        "toast",
        "oat",
        "bread",
        "fruit",
        "banana",
        "apple",
        "berries",
        "tofu",
        "egg",
        "avocado",
        "nut",
        "smoothie",
        "cereal",
        "rice",
        "pancake",
        "porridge",
        "granola",
        "tomato",
        "potato",
        "vegetable",
    ]
    assert any(s in lowered for s in food_signals), (
        f"reply does not look like a food plan; got: {out!r}"
    )


async def test_plan_with_tool_and_memory(tmp_path, ollama_base_url: str, ollama_model: str) -> None:  # type: ignore[no-untyped-def]
    """The model has to: (a) remember a fact from turn 1, (b) call a tool
    in turn 2, (c) combine both in turn 3.

    The tool returns a unique deadline string. The plan in turn 3 must
    mention both the user's preferred work hours (memory) AND the deadline
    string (tool output)."""

    deadline = f"2026-04-30T17:00 KST [{uuid.uuid4().hex[:6]}]"

    class _DeadlineArgs(BaseModel):
        model_config = {"extra": "forbid"}

    async def _deadline_handler(_: _DeadlineArgs) -> str:
        return deadline

    deadline_tool = FunctionTool(
        name="get_project_deadline",
        description=(
            "Return the user's current project deadline as an ISO datetime "
            "string. Call this whenever the user mentions a deadline or "
            "asks you to plan around one."
        ),
        input_model=_DeadlineArgs,
        handler=_deadline_handler,
    )

    paths = SampyclawPaths(home=tmp_path)
    paths.ensure_home()
    tools = ToolRegistry()
    tools.register_all(default_tools())
    tools.register(deadline_tool)

    agent = _build_agent(paths=paths, base_url=ollama_base_url, model=ollama_model, tools=tools)
    try:
        ctx = AgentContext(agent_id=agent.id, session_key="plan-tool")
        # Turn 1 — fact to remember.
        await _collect(
            agent,
            _envelope("Remember: I prefer to work in the morning, between 9am and noon."),
            ctx,
        )
        # Turn 2 — explicit tool invocation request.
        await _collect(
            agent,
            _envelope("Use the get_project_deadline tool to look up my deadline."),
            ctx,
        )
        # Turn 3 — synthesis.
        out = await _collect(
            agent,
            _envelope(
                "Given my deadline and my preferred work hours, propose a "
                "two-day work plan. Include the deadline string verbatim "
                "and reference my preferred hours."
            ),
            ctx,
        )
    finally:
        await agent.aclose()

    assert deadline in out, f"plan missing tool-derived deadline {deadline!r}; got: {out!r}"
    lowered = out.lower()
    has_morning_signal = any(
        kw in lowered for kw in ("morning", "9am", "9 am", "9:00", "noon", "9-noon", "9am-noon")
    )
    assert has_morning_signal, f"plan does not reference remembered work hours; got: {out!r}"
