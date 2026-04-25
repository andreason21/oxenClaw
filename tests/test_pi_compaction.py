"""Phase 5: compaction pipeline tests."""

from __future__ import annotations

import pytest

from sampyclaw.pi import (
    AssistantMessage,
    CreateAgentSessionOptions,
    InMemorySessionManager,
    SystemMessage,
    TextContent,
    ToolResultBlock,
    ToolResultMessage,
    ToolUseBlock,
    UserMessage,
    text_message,
)
from sampyclaw.pi.compaction import (
    apply_compaction,
    decide_compaction,
    maybe_compact,
    truncating_summarizer,
)


def _make_history(n_user_turns: int = 20, content_size: int = 200):  # type: ignore[no-untyped-def]
    msgs = [SystemMessage(content="be brief")]
    for i in range(n_user_turns):
        msgs.append(UserMessage(content=f"u{i} " + "x" * content_size))
        msgs.append(
            AssistantMessage(
                content=[TextContent(text=f"a{i} " + "y" * content_size)],
                stop_reason="end_turn",
            )
        )
    return msgs


def test_decide_skip_when_under_threshold() -> None:
    msgs = _make_history(n_user_turns=2)
    plan = decide_compaction(
        msgs, model_context_tokens=100_000, threshold_ratio=0.85
    )
    assert plan.needed is False
    assert plan.tokens_before > 0


def test_decide_picks_safe_boundary_under_pressure() -> None:
    msgs = _make_history(n_user_turns=20)
    plan = decide_compaction(
        msgs, model_context_tokens=2_000, threshold_ratio=0.85, keep_tail_turns=4
    )
    assert plan.needed is True
    assert plan.keep_tail_count >= 4
    # Drop indexes always start at 0 and form a contiguous prefix.
    assert plan.drop_indexes[0] == 0
    assert all(
        b == a + 1 for a, b in zip(plan.drop_indexes, plan.drop_indexes[1:])
    )


def test_decide_avoids_orphaning_tool_result() -> None:
    """Boundary must not split an assistant(tool_use) → tool_result pair."""
    msgs = [
        SystemMessage(content="s"),
        UserMessage(content="hi"),
        AssistantMessage(
            content=[
                TextContent(text="calling"),
                ToolUseBlock(id="t1", name="echo", input={}),
            ],
            stop_reason="tool_use",
        ),
        ToolResultMessage(
            results=[ToolResultBlock(tool_use_id="t1", content="ok")]
        ),
        AssistantMessage(
            content=[TextContent(text="done")], stop_reason="end_turn"
        ),
    ]
    # Force a tail of 1 → would otherwise split between assistant and
    # tool_result. The picker should walk forward to avoid that.
    plan = decide_compaction(
        msgs, model_context_tokens=100, keep_tail_turns=1, force=True
    )
    if plan.needed and plan.drop_indexes:
        last_drop = max(plan.drop_indexes)
        # Index right after last_drop must not be a tool_result.
        assert not isinstance(msgs[last_drop + 1], ToolResultMessage)


async def test_apply_compaction_replaces_prefix_with_summary() -> None:
    msgs = _make_history(n_user_turns=10)
    plan = decide_compaction(
        msgs, model_context_tokens=1_500, keep_tail_turns=4, force=True
    )
    assert plan.needed
    new_msgs, entry = await apply_compaction(msgs, plan, truncating_summarizer)
    assert isinstance(new_msgs[0], SystemMessage)
    assert "COMPACTED SUMMARY" in new_msgs[0].content
    assert entry.tokens_after < entry.tokens_before
    assert entry.replaced_message_indexes == plan.drop_indexes


async def test_maybe_compact_in_place_updates_session() -> None:
    sm = InMemorySessionManager()
    s = await sm.create(CreateAgentSessionOptions(agent_id="local"))
    s.messages = _make_history(n_user_turns=12)
    before = len(s.messages)
    did = await maybe_compact(
        s,
        model_context_tokens=1_500,
        summarizer=truncating_summarizer,
        keep_tail_turns=4,
    )
    assert did is True
    assert len(s.messages) < before
    assert len(s.compactions) == 1
    assert s.compactions[0].reason == "auto"


async def test_maybe_compact_skips_when_under_threshold() -> None:
    sm = InMemorySessionManager()
    s = await sm.create(CreateAgentSessionOptions(agent_id="local"))
    s.messages = _make_history(n_user_turns=2)
    did = await maybe_compact(
        s, model_context_tokens=100_000, summarizer=truncating_summarizer
    )
    assert did is False
    assert s.compactions == []


async def test_truncating_summarizer_includes_first_user_and_last_assistant() -> None:
    msgs = [
        UserMessage(content="initial question about deployment"),
        AssistantMessage(
            content=[TextContent(text="middle answer")], stop_reason="end_turn"
        ),
        UserMessage(content="follow-up"),
        AssistantMessage(
            content=[TextContent(text="final wrap-up answer")],
            stop_reason="end_turn",
        ),
    ]
    s = await truncating_summarizer(msgs)
    assert "initial question" in s
    assert "final wrap-up" in s
    assert "4 messages compacted" in s
