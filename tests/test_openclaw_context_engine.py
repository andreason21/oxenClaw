"""OpenclawContextEngine — proactive trim behaviour."""

from __future__ import annotations

from oxenclaw.pi.context_engine import OpenclawContextEngine
from oxenclaw.pi.messages import (
    AssistantMessage,
    TextContent,
    ToolResultBlock,
    ToolResultMessage,
    UserMessage,
)


def _msgs_with_huge_tool_result(content_chars: int) -> list:
    return [
        UserMessage(content="please run a search"),
        AssistantMessage(
            content=[TextContent(text="searching")],
            stop_reason="tool_use",
        ),
        ToolResultMessage(
            results=[
                ToolResultBlock(
                    tool_use_id="t1",
                    content="X" * content_chars,
                )
            ]
        ),
    ]


async def test_passthrough_when_under_threshold() -> None:
    """Below 80% of budget: messages should be returned unchanged."""
    engine = OpenclawContextEngine()
    msgs = _msgs_with_huge_tool_result(content_chars=2_000)
    result = await engine.assemble(
        session_id="s",
        messages=msgs,
        token_budget=100_000,  # tiny tool result vs huge budget → no trim
    )
    # ToolResultBlock.content unchanged
    tr = result.messages[2]
    assert isinstance(tr, ToolResultMessage)
    assert len(tr.results[0].content or "") == 2_000


async def test_proactive_trim_when_over_threshold() -> None:
    """Over 80% of budget: tool_result content trimmed to keep_chars."""
    engine = OpenclawContextEngine()
    # 50 KB tool result vs 8000-token budget. char/3.5 → ~14000 tokens
    # estimated, well over 80%×8000=6400.
    msgs = _msgs_with_huge_tool_result(content_chars=50_000)
    result = await engine.assemble(
        session_id="s",
        messages=msgs,
        token_budget=8_000,
    )
    tr = result.messages[2]
    assert isinstance(tr, ToolResultMessage)
    trimmed = tr.results[0].content or ""
    assert len(trimmed) <= engine.proactive_keep_chars + 64  # cap + sentinel
    assert "trimmed" in trimmed


async def test_no_token_budget_means_no_trim() -> None:
    engine = OpenclawContextEngine()
    msgs = _msgs_with_huge_tool_result(content_chars=50_000)
    result = await engine.assemble(
        session_id="s",
        messages=msgs,
        token_budget=None,
    )
    tr = result.messages[2]
    assert isinstance(tr, ToolResultMessage)
    assert len(tr.results[0].content or "") == 50_000


async def test_pi_agent_default_engine_is_openclaw() -> None:
    """PiAgent must default to OpenclawContextEngine when no engine
    is injected — establishes openclaw parity."""
    # Lightweight imports to avoid PiAgent's full init paths
    # Read the relevant default path from the source rather than
    # constructing a full PiAgent (which needs a model registry).
    import inspect

    from oxenclaw.agents.pi_agent import PiAgent
    from oxenclaw.pi.context_engine import OpenclawContextEngine as _OpenclawCE

    src = inspect.getsource(PiAgent.__init__)
    assert "OpenclawContextEngine()" in src, "PiAgent should default to OpenclawContextEngine"
    assert _OpenclawCE is not None
