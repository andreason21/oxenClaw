"""End-to-end: when the model writes a tool call as JSON in its
reply text instead of issuing a real `tool_use` block, PiAgent
auto-fires the tool and runs one more round so the user sees a
grounded final answer.

Mirrors the actual failure transcript that motivated this path:
    user: "지금 깔려있는 weather tool 을 사용해"
    model (turn 1): "수원 날씨를 확인하겠습니다."
                    ```json
                    {"tool":"weather","location":"Suwon, South Korea"}
                    ```
                    — no real tool_use → nothing fires → next user
                    "진행해" had nothing to act on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, model_validator

from oxenclaw.acp import manager as manager_mod
from oxenclaw.acp import runtime_registry as registry_mod
from oxenclaw.acp.manager import (
    AcpInitializeSessionInput,
    AcpRunTurnInput,
    get_acp_session_manager,
)
from oxenclaw.acp.pi_agent_runtime import PiAgentAcpRuntime
from oxenclaw.acp.runtime_registry import (
    AcpRuntimeBackend,
    register_acp_runtime_backend,
)
from oxenclaw.agents.acp_runtime import (
    AcpEventTextDelta,
    AcpRuntimeEvent,
)
from oxenclaw.agents.pi_agent import PiAgent
from oxenclaw.agents.tools import FunctionTool, ToolRegistry
from oxenclaw.config import OxenclawPaths
from oxenclaw.pi import (
    InMemoryAuthStorage,
    InMemorySessionManager,
    Model,
    register_provider_stream,
)
from oxenclaw.pi.registry import InMemoryModelRegistry
from oxenclaw.pi.streaming import StopEvent, TextDeltaEvent


@pytest.fixture(autouse=True)
def _isolate_globals():
    registry_mod.reset_for_tests()
    manager_mod.reset_for_tests()
    yield
    registry_mod.reset_for_tests()
    manager_mod.reset_for_tests()


def _paths(tmp_path: Path) -> OxenclawPaths:
    p = OxenclawPaths(home=tmp_path)
    p.ensure_home()
    return p


class _WeatherArgs(BaseModel):
    """Stub mirrors the real `oxenclaw.tools_pkg.weather._WeatherArgs`
    `_absorb_aliases` validator that folds the common `location` /
    `query` drift onto the canonical `city` field — the auto-fire
    path passes args through unchanged, so the same drift-absorption
    that makes real tool_use calls work also has to make pseudo-tool
    auto-fires work."""

    city: str

    @model_validator(mode="before")
    @classmethod
    def _absorb_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if not out.get("city"):
            for alias in ("location", "place", "query"):
                v = out.get(alias)
                if isinstance(v, str) and v.strip():
                    out["city"] = v.strip()
                    break
        for k in ("location", "place", "query"):
            out.pop(k, None)
        return out


def _build_tools(*, weather_log: list[dict[str, str]]) -> ToolRegistry:
    async def _weather(args: _WeatherArgs) -> str:
        weather_log.append({"city": args.city})
        return f"Sunny 20°C in {args.city}"

    tools = ToolRegistry()
    tools.register(
        FunctionTool(
            name="weather",
            description="Look up current weather at a city.",
            input_model=_WeatherArgs,
            handler=_weather,
        )
    )
    return tools


async def _drain(it: AsyncIterator[AcpRuntimeEvent]) -> list[AcpRuntimeEvent]:
    out: list[AcpRuntimeEvent] = []
    async for ev in it:
        out.append(ev)
    return out


async def test_pseudo_tool_in_text_is_auto_fired_and_summarised(
    tmp_path: Path,
) -> None:
    """The fake stream simulates a small local model that writes a
    fenced JSON tool call in text on the first round, then on the
    second round (after the runtime's auto-fire injects a real tool
    result) summarises the actual data. The end-user assertion: the
    weather tool DID fire with `city="Suwon"`, and the final reply
    text references the result."""

    weather_log: list[dict[str, str]] = []
    tools = _build_tools(weather_log=weather_log)

    state = {"calls": 0}

    async def fake_stream(ctx, _opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        # ----- Round 1: the broken behaviour. Model emits text only,
        # with the tool call buried in a fenced JSON block. -----
        if state["calls"] == 1:
            yield TextDeltaEvent(delta="수원 날씨를 확인하겠습니다.\n\n")
            yield TextDeltaEvent(
                delta=('```json\n{"tool": "weather", "location": "Suwon, South Korea"}\n```')
            )
            yield StopEvent(reason="end_turn")
            return
        # ----- Round 2: PiAgent's auto-fire path injected a real
        # synthetic ToolUseBlock + ToolResultMessage; this round is
        # the model summarising the result. -----
        yield TextDeltaEvent(delta="수원은 현재 맑고 20도입니다.")
        yield StopEvent(reason="end_turn")

    register_provider_stream("pseudo_autofire", fake_stream)
    reg = InMemoryModelRegistry(
        models=[
            Model(
                id="pseudo-autofire",
                provider="pseudo_autofire",
                max_output_tokens=512,
                extra={"base_url": "http://test-fake"},
            )
        ]
    )
    agent = PiAgent(
        agent_id="pseudo-autofire",
        model_id="pseudo-autofire",
        registry=reg,
        auth=InMemoryAuthStorage({"pseudo_autofire": "sk-test"}),  # type: ignore[dict-item]
        sessions=InMemorySessionManager(),
        tools=tools,
        paths=_paths(tmp_path),
        memory=None,  # memory off — this test is about auto-fire only
        memory_inject_into_user=False,
    )
    runtime = PiAgentAcpRuntime(agent=agent, backend_id="pi")
    register_acp_runtime_backend(AcpRuntimeBackend(id="pi", runtime=runtime))
    mgr = get_acp_session_manager()

    await mgr.initialize_session(
        AcpInitializeSessionInput(
            session_key="autofire",
            agent="pseudo-autofire",
            mode="persistent",
            backend_id="pi",
        )
    )
    events = await _drain(
        mgr.run_turn(
            AcpRunTurnInput(
                session_key="autofire",
                text="weather 툴 사용해",
                request_id="r-1",
            )
        )
    )

    # The auto-fire path actually executed the weather tool, with the
    # `location` alias folded by our stub model's `model_post_init`
    # (mirrors the production `_absorb_aliases` validator on the real
    # weather tool).
    assert weather_log == [{"city": "Suwon, South Korea"}], (
        f"weather tool was not auto-fired; weather_log={weather_log!r}"
    )

    # The final user-visible reply is the round-2 summary, NOT the
    # round-1 text that contained the JSON block. Both rounds happened,
    # but the summary supersedes the original promise.
    text_deltas = [e for e in events if isinstance(e, AcpEventTextDelta)]
    full_text = "".join(t.text for t in text_deltas)
    # Round-1 commentary may still appear (it's the model's own text),
    # but the round-2 grounded summary MUST also be present.
    assert "수원은 현재 맑고 20도" in full_text, (
        f"final reply did not include the post-auto-fire summary: {full_text!r}"
    )

    # The fake stream was invoked exactly twice — once for the broken
    # round, once for the post-auto-fire summary. Three would mean we
    # accidentally re-triggered the auto-fire on the summary turn.
    assert state["calls"] == 2, (
        f"expected exactly 2 model rounds (broken + summary), got {state['calls']}"
    )


async def test_no_autofire_when_text_has_no_pseudo_call(tmp_path: Path) -> None:
    """A normal text-only reply with no JSON should NOT trigger an
    extra round — guarding against the regression of doubled model
    calls on every turn."""

    weather_log: list[dict[str, str]] = []
    tools = _build_tools(weather_log=weather_log)

    state = {"calls": 0}

    async def fake_stream(ctx, _opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        yield TextDeltaEvent(delta="안녕하세요, 무엇을 도와드릴까요?")
        yield StopEvent(reason="end_turn")

    register_provider_stream("pseudo_no_call", fake_stream)
    reg = InMemoryModelRegistry(
        models=[
            Model(
                id="pseudo-no-call",
                provider="pseudo_no_call",
                max_output_tokens=128,
                extra={"base_url": "http://test-fake"},
            )
        ]
    )
    agent = PiAgent(
        agent_id="pseudo-no-call",
        model_id="pseudo-no-call",
        registry=reg,
        auth=InMemoryAuthStorage({"pseudo_no_call": "sk-test"}),  # type: ignore[dict-item]
        sessions=InMemorySessionManager(),
        tools=tools,
        paths=_paths(tmp_path),
        memory=None,
        memory_inject_into_user=False,
    )
    runtime = PiAgentAcpRuntime(agent=agent, backend_id="pi")
    register_acp_runtime_backend(AcpRuntimeBackend(id="pi", runtime=runtime))
    mgr = get_acp_session_manager()

    await mgr.initialize_session(
        AcpInitializeSessionInput(
            session_key="nocall",
            agent="pseudo-no-call",
            mode="persistent",
            backend_id="pi",
        )
    )
    await _drain(
        mgr.run_turn(
            AcpRunTurnInput(
                session_key="nocall",
                text="안녕",
                request_id="r-1",
            )
        )
    )

    assert weather_log == []
    assert state["calls"] == 1, (
        f"expected exactly 1 model round, got {state['calls']} — auto-fire "
        "must not trigger on plain text replies"
    )


async def test_multi_turn_pseudo_then_proceed_does_not_double_fire(
    tmp_path: Path,
) -> None:
    """Real multi-turn exercise of the user-reported failure:
       turn 1 user: "weather 툴 사용해"
       turn 1 model: pseudo-JSON tool call → B auto-fires + summarises
       turn 2 user: "진행해"
       turn 2 model: should NOT re-fire weather (B already handled it)
                     and should NOT have C-prelude inserted (the prior
                     turn's promise was already fulfilled by auto-fire,
                     so a real ToolUseBlock is in transcript).

    Locks the desired property: B fires once on turn 1, and the
    "진행해" follow-up flows normally without any additional weather
    invocations or pending-action injection."""

    weather_log: list[dict[str, str]] = []
    tools = _build_tools(weather_log=weather_log)
    seen_user_messages: list[str] = []
    state = {"calls": 0}

    async def fake_stream(ctx, _opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        # Capture what the model actually saw on each round.
        for msg in reversed(getattr(ctx, "messages", [])):
            if getattr(msg, "role", None) == "user":
                content = getattr(msg, "content", None)
                seen_user_messages.append(
                    content
                    if isinstance(content, str)
                    else "\n".join(
                        getattr(b, "text", "")
                        for b in content
                        if getattr(b, "type", None) == "text"
                    )
                )
                break

        if state["calls"] == 1:
            # Round 1: pseudo tool call in text.
            yield TextDeltaEvent(delta="수원 날씨를 확인하겠습니다.\n\n")
            yield TextDeltaEvent(delta='```json\n{"tool":"weather","location":"Suwon"}\n```')
            yield StopEvent(reason="end_turn")
            return
        if state["calls"] == 2:
            # Round 2: post auto-fire summary (still inside turn 1).
            yield TextDeltaEvent(delta="수원은 현재 맑고 20도입니다.")
            yield StopEvent(reason="end_turn")
            return
        # Turn 2: user said "진행해". The transcript already carries
        # the synthetic ToolUseBlock + result + summary, so neither B
        # nor C should fire. Reply naturally.
        yield TextDeltaEvent(delta="네, 더 도와드릴 일이 있을까요?")
        yield StopEvent(reason="end_turn")

    register_provider_stream("multi_turn_autofire", fake_stream)
    reg = InMemoryModelRegistry(
        models=[
            Model(
                id="multi-autofire",
                provider="multi_turn_autofire",
                max_output_tokens=512,
                extra={"base_url": "http://test-fake"},
            )
        ]
    )
    agent = PiAgent(
        agent_id="multi-autofire",
        model_id="multi-autofire",
        registry=reg,
        auth=InMemoryAuthStorage({"multi_turn_autofire": "sk-test"}),  # type: ignore[dict-item]
        sessions=InMemorySessionManager(),
        tools=tools,
        paths=_paths(tmp_path),
        memory=None,
        memory_inject_into_user=False,
    )
    runtime = PiAgentAcpRuntime(agent=agent, backend_id="pi")
    register_acp_runtime_backend(AcpRuntimeBackend(id="pi", runtime=runtime))
    mgr = get_acp_session_manager()

    await mgr.initialize_session(
        AcpInitializeSessionInput(
            session_key="multi",
            agent="multi-autofire",
            mode="persistent",
            backend_id="pi",
        )
    )

    # ===== TURN 1 =====
    await _drain(
        mgr.run_turn(
            AcpRunTurnInput(
                session_key="multi",
                text="weather 툴 사용해",
                request_id="r-1",
            )
        )
    )
    assert weather_log == [{"city": "Suwon"}], (
        f"turn1 auto-fire didn't run weather; weather_log={weather_log!r}"
    )
    assert state["calls"] == 2, (
        f"turn1 expected 2 model rounds (broken + summary), got {state['calls']}"
    )

    # ===== TURN 2 =====
    turn2_events = await _drain(
        mgr.run_turn(
            AcpRunTurnInput(
                session_key="multi",
                text="진행해",
                request_id="r-2",
            )
        )
    )

    # B must NOT re-fire weather on turn 2 — the original tool result
    # is already in the transcript, so weather_log stays at 1 entry.
    assert weather_log == [{"city": "Suwon"}], (
        f"weather re-fired on turn2; weather_log={weather_log!r}"
    )
    # Exactly one more model round on turn 2 (no auto-fire retry).
    assert state["calls"] == 3, f"turn2 expected 1 round (=3 total), got {state['calls']}"

    # C's pending-action prelude must NOT have been injected: the
    # synthetic auto-fired ToolUseBlock counts as a real tool call,
    # so `extract_unfulfilled_promise` returns None and the user's
    # "진행해" reaches the model unchanged.
    turn2_user_seen = seen_user_messages[-1]
    assert "PENDING ACTION" not in turn2_user_seen, (
        f"C-prelude wrongly injected on turn2: {turn2_user_seen!r}"
    )
    assert turn2_user_seen.strip() == "진행해", (
        f"turn2 user text was modified unexpectedly: {turn2_user_seen!r}"
    )

    text_deltas = [e for e in turn2_events if isinstance(e, AcpEventTextDelta)]
    full_text = "".join(t.text for t in text_deltas)
    assert "도와드릴" in full_text


async def test_multi_turn_promise_only_then_proceed_triggers_c_prelude(
    tmp_path: Path,
) -> None:
    """B's residual case: model only narrated an intent on turn 1
    (no JSON to parse), so B couldn't auto-fire. On turn 2 the user
    says "진행해" and C's prelude should kick in — informing the
    model that its prior promise was unfulfilled and prompting a
    real tool_use call this time.

    This covers the failure mode the user reported: the agent
    'forgets' to actually do the thing it said it would."""

    weather_log: list[dict[str, str]] = []
    tools = _build_tools(weather_log=weather_log)
    seen_user_messages: list[str] = []
    state = {"calls": 0}

    async def fake_stream(ctx, _opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        for msg in reversed(getattr(ctx, "messages", [])):
            if getattr(msg, "role", None) == "user":
                content = getattr(msg, "content", None)
                seen_user_messages.append(
                    content
                    if isinstance(content, str)
                    else "\n".join(
                        getattr(b, "text", "")
                        for b in content
                        if getattr(b, "type", None) == "text"
                    )
                )
                break

        if state["calls"] == 1:
            # Turn 1: bare narrative promise, no JSON. B cannot fire.
            yield TextDeltaEvent(delta="수원 날씨를 확인하겠습니다.")
            yield StopEvent(reason="end_turn")
            return
        # Turn 2: now that the C-prelude has been injected, the
        # model "wakes up" and fires the real tool. We assert the
        # prelude actually reached this round by inspecting what
        # the stream saw.
        from oxenclaw.pi.streaming import ToolUseEndEvent, ToolUseInputDeltaEvent, ToolUseStartEvent

        if state["calls"] == 2:
            yield ToolUseStartEvent(id="w_real_1", name="weather")
            yield ToolUseInputDeltaEvent(id="w_real_1", input_delta='{"city":"Suwon"}')
            yield ToolUseEndEvent(id="w_real_1")
            yield StopEvent(reason="tool_use")
            return
        # Round 3: post-tool summary inside turn 2.
        yield TextDeltaEvent(delta="수원은 맑고 20도입니다.")
        yield StopEvent(reason="end_turn")

    register_provider_stream("multi_promise_c", fake_stream)
    reg = InMemoryModelRegistry(
        models=[
            Model(
                id="multi-promise-c",
                provider="multi_promise_c",
                max_output_tokens=512,
                extra={"base_url": "http://test-fake"},
            )
        ]
    )
    agent = PiAgent(
        agent_id="multi-promise-c",
        model_id="multi-promise-c",
        registry=reg,
        auth=InMemoryAuthStorage({"multi_promise_c": "sk-test"}),  # type: ignore[dict-item]
        sessions=InMemorySessionManager(),
        tools=tools,
        paths=_paths(tmp_path),
        memory=None,
        memory_inject_into_user=False,
    )
    runtime = PiAgentAcpRuntime(agent=agent, backend_id="pi")
    register_acp_runtime_backend(AcpRuntimeBackend(id="pi", runtime=runtime))
    mgr = get_acp_session_manager()

    await mgr.initialize_session(
        AcpInitializeSessionInput(
            session_key="promise",
            agent="multi-promise-c",
            mode="persistent",
            backend_id="pi",
        )
    )

    # ===== TURN 1: narrative promise only, no JSON. =====
    await _drain(
        mgr.run_turn(
            AcpRunTurnInput(
                session_key="promise",
                text="수원 날씨 확인해줘",
                request_id="r-1",
            )
        )
    )
    assert weather_log == [], "B should not have fired (no JSON in turn 1 reply)"
    assert state["calls"] == 1

    # ===== TURN 2: "진행해" — C should inject the prelude. =====
    await _drain(
        mgr.run_turn(
            AcpRunTurnInput(
                session_key="promise",
                text="진행해",
                request_id="r-2",
            )
        )
    )

    # The model on call #2 must have seen the C-prelude attached to
    # the user message — that is the contract that lets a small
    # model recover the dropped action.
    turn2_user_seen = seen_user_messages[1]
    assert "PENDING ACTION" in turn2_user_seen, (
        f"C-prelude was not injected on turn2: {turn2_user_seen!r}"
    )
    assert "확인하겠습니다" in turn2_user_seen, (
        "C-prelude must echo the prior promise snippet so the model "
        f"can identify what to do; saw: {turn2_user_seen!r}"
    )

    # And the model's resulting tool call actually fired.
    assert weather_log == [{"city": "Suwon"}], (
        f"weather did not fire after C-prelude; weather_log={weather_log!r}"
    )
