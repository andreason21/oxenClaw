"""Anti-thrash guard for the compaction pipeline.

Verifies the guard tells `decide_compaction` to skip when recent passes
barely shrank the prompt — and that one big save resets the streak.
"""

from __future__ import annotations

from oxenclaw.pi.compaction import (
    CompactionGuard,
    decide_compaction,
    should_skip_compaction,
)
from oxenclaw.pi.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    UserMessage,
)


def _big_history(turns: int = 10, content_size: int = 400) -> list:
    msgs: list = [SystemMessage(content="be brief")]
    for i in range(turns):
        msgs.append(UserMessage(content=f"u{i} " + "x" * content_size))
        msgs.append(
            AssistantMessage(
                content=[TextContent(text=f"a{i} " + "y" * content_size)],
                stop_reason="end_turn",
            )
        )
    return msgs


def test_guard_progressive_shrinks_no_skip() -> None:
    """When each compaction makes a meaningful dent, the guard never skips."""
    g = CompactionGuard()
    g.record(10_000, 6_000)  # saved 40%
    g.record(6_000, 3_500)  # saved ~42%
    assert should_skip_compaction(g, current_size=3_500, threshold_pct=10.0) is False


def test_guard_two_flat_compactions_triggers_skip() -> None:
    """Two consecutive low-savings passes → skip the next."""
    g = CompactionGuard()
    g.record(10_000, 9_700)  # saved 3%
    g.record(9_700, 9_500)  # saved ~2%
    assert should_skip_compaction(g, current_size=9_500, threshold_pct=10.0) is True


def test_guard_resets_on_big_save() -> None:
    """A flat pass followed by a big save means progress is happening
    again and the next pass should NOT be skipped."""
    g = CompactionGuard()
    g.record(10_000, 9_900)  # 1% — flat
    g.record(9_900, 5_000)   # 49% — big save
    assert should_skip_compaction(g, current_size=5_000, threshold_pct=10.0) is False


def test_guard_first_time_always_proceeds() -> None:
    """Fresh guard with < 2 history entries must never block compaction."""
    g = CompactionGuard()
    assert should_skip_compaction(g, current_size=10_000, threshold_pct=10.0) is False
    g.record(10_000, 9_500)  # only one entry recorded
    assert should_skip_compaction(g, current_size=9_500, threshold_pct=10.0) is False


def test_decide_compaction_honours_guard_skip() -> None:
    """When the guard says skip, decide_compaction returns needed=False."""
    msgs = _big_history(turns=10)
    g = CompactionGuard()
    # Two near-no-op passes recorded — guard should now block compaction.
    g.record(10_000, 9_950)
    g.record(9_950, 9_900)
    plan = decide_compaction(
        msgs,
        model_context_tokens=2_000,
        threshold_ratio=0.5,
        keep_tail_turns=4,
        guard=g,
    )
    # Even though token count is well over threshold, guard blocks the pass.
    assert plan.needed is False
