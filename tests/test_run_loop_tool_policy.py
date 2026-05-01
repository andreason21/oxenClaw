"""EffectiveToolPolicy auto-wiring at run_agent_turn boundary."""

from __future__ import annotations

from pydantic import BaseModel

import oxenclaw.pi.providers  # noqa: F401
from oxenclaw.agents.tools import FunctionTool, ToolRegistry
from oxenclaw.pi import (
    InMemoryAuthStorage,
    Model,
    register_provider_stream,
    resolve_api,
)
from oxenclaw.pi.registry import InMemoryModelRegistry
from oxenclaw.pi.run import RuntimeConfig, run_agent_turn
from oxenclaw.pi.streaming import (
    StopEvent,
    TextDeltaEvent,
    ToolUseEndEvent,
    ToolUseInputDeltaEvent,
    ToolUseStartEvent,
)
from oxenclaw.pi.tool_runtime import (
    EffectiveToolPolicy,
    ToolNameAllowlist,
    ToolOverride,
)


class _N(BaseModel):
    pass


def _make_tool(name: str, output: str) -> FunctionTool:
    return FunctionTool(
        name=name,
        description=name,
        input_model=_N,
        handler=lambda _a, out=output: out,
    )


async def test_tool_policy_filters_disabled_tools() -> None:
    """A tool with `enabled=False` should be invisible to the model
    (the run loop receives a filtered tool list)."""

    captured: dict[str, list[str]] = {"tools_seen": []}

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        captured["tools_seen"] = [t.name for t in ctx.tools]
        yield TextDeltaEvent(delta="ok")
        yield StopEvent(reason="end_turn")

    register_provider_stream("polfilter", fake_stream)
    reg = InMemoryModelRegistry(
        models=[Model(id="m", provider="polfilter", max_output_tokens=64, extra={"base_url": "x"})]
    )
    model = reg.list()[0]
    api = await resolve_api(model, InMemoryAuthStorage({"polfilter": "x"}))  # type: ignore[dict-item]

    tools = ToolRegistry()
    tools.register(_make_tool("alpha", "alpha-out"))
    tools.register(_make_tool("beta", "beta-out"))

    policy = EffectiveToolPolicy(
        overrides=(ToolOverride(name="beta", enabled=False),),
    )
    cfg = RuntimeConfig(tool_policy=policy, max_tool_iterations=2)
    await run_agent_turn(
        model=model,
        api=api,
        system=None,
        history=[],
        tools=list(tools._tools.values()),
        config=cfg,
    )
    assert captured["tools_seen"] == ["alpha"]


async def test_tool_policy_truncates_oversize_results() -> None:
    """`max_chars_for` clamps a noisy tool's output before it lands in
    the next turn's context."""
    big_text = "X" * 10_000

    state = {"calls": 0}

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if state["calls"] == 1:
            tid = "t1"
            yield ToolUseStartEvent(id=tid, name="loud")
            yield ToolUseInputDeltaEvent(id=tid, input_delta="{}")
            yield ToolUseEndEvent(id=tid)
            yield StopEvent(reason="tool_use")
        else:
            yield TextDeltaEvent(delta="done")
            yield StopEvent(reason="end_turn")

    register_provider_stream("poltrunc", fake_stream)
    reg = InMemoryModelRegistry(
        models=[Model(id="m", provider="poltrunc", max_output_tokens=64, extra={"base_url": "x"})]
    )
    model = reg.list()[0]
    api = await resolve_api(model, InMemoryAuthStorage({"poltrunc": "x"}))  # type: ignore[dict-item]

    tools = ToolRegistry()
    tools.register(_make_tool("loud", big_text))

    policy = EffectiveToolPolicy(
        allowlist=ToolNameAllowlist(),
        overrides=(ToolOverride(name="loud", max_result_chars=200),),
    )
    cfg = RuntimeConfig(tool_policy=policy, max_tool_iterations=4)
    result = await run_agent_turn(
        model=model,
        api=api,
        system=None,
        history=[],
        tools=list(tools._tools.values()),
        config=cfg,
    )
    # Find the ToolResultMessage in appended; the truncated content
    # must be at most cap + sentinel length.
    from oxenclaw.pi.messages import ToolResultMessage

    # The smart truncator (port of openclaw `tool-result-truncation.ts`)
    # uses an informative suffix that's longer than the legacy
    # "[...truncated N chars]" sentinel. With an explicit cap of 200,
    # the body collapses to roughly the suffix alone — well under the
    # original 10K input but above the legacy tight bound. We just
    # verify truncation happened and the result is a small fraction of
    # the original.
    found = False
    for m in result.appended_messages:
        if isinstance(m, ToolResultMessage):
            for r in m.results:
                if r.tool_use_id == "t1":
                    found = True
                    assert "truncated" in (r.content or "")
                    assert len(r.content or "") <= 1_000  # << 10K original
    assert found
