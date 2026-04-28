"""Tests for the structured LLM-based summariser pipeline.

Mirrors hermes-agent's Phase-2 quality-leap context compactor.
"""

from __future__ import annotations

import json

import pytest

from oxenclaw.pi.compaction import (
    SUMMARY_PREFIX,
    CompactionGuard,
    _align_boundary_backward,
    _dedup_tool_results_by_md5,
    _ensure_last_user_message_in_tail,
    _sanitize_tool_pairs,
    _summarize_tool_result,
    _truncate_tool_call_args_json,
    llm_structured_summarizer,
    structured_summarizer_pipeline,
)
from oxenclaw.pi.messages import (
    AssistantMessage,
    SystemMessage,
    TextContent,
    ToolResultBlock,
    ToolResultMessage,
    ToolUseBlock,
    UserMessage,
)

# ─── Helper builders ────────────────────────────────────────────────


def _user(text: str) -> UserMessage:
    return UserMessage(content=text)


def _assistant_text(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        stop_reason="end_turn",
    )


def _assistant_tool(tool_id: str, name: str, args: dict | None = None) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolUseBlock(id=tool_id, name=name, input=args or {})],
        stop_reason="tool_use",
    )


def _tool_result(tool_id: str, body: str) -> ToolResultMessage:
    return ToolResultMessage(results=[ToolResultBlock(tool_use_id=tool_id, content=body)])


# ─── _summarize_tool_result ─────────────────────────────────────────


def test_summarize_tool_result_known_names() -> None:
    out = _summarize_tool_result("read_file", "x" * 3400)
    assert "read_file" in out
    assert "3,400" in out

    out = _summarize_tool_result("shell", "line1\nline2\nline3\n")
    assert "shell" in out
    assert "lines output" in out

    out = _summarize_tool_result("memory_search", "hit1\nhit2\nhit3")
    assert "memory_search" in out
    assert "hits" in out


def test_summarize_tool_result_unknown_falls_back() -> None:
    out = _summarize_tool_result("nonexistent_tool_xyz", "abc def")
    assert "nonexistent_tool_xyz" in out
    assert "tool result" in out


# ─── _truncate_tool_call_args_json ──────────────────────────────────


def test_truncate_tool_call_args_preserves_short() -> None:
    args = json.dumps({"path": "/tmp/foo", "n": 1})
    assert _truncate_tool_call_args_json(args, max_chars=4000) == args


def test_truncate_tool_call_args_shrinks_long_strings() -> None:
    huge = "x" * 20000
    args = json.dumps({"path": "/tmp/foo", "content": huge})
    result = _truncate_tool_call_args_json(args, max_chars=4000)
    # Output must be valid JSON.
    parsed = json.loads(result)
    assert parsed["path"] == "/tmp/foo"
    # The huge string is shrunk and marked.
    assert "...(truncated)" in parsed["content"]
    assert len(result) < len(args)


def test_truncate_tool_call_args_invalid_json_fallback() -> None:
    bad = "this is not json " + "x" * 5000
    result = _truncate_tool_call_args_json(bad, max_chars=4000)
    # Must still be valid JSON output.
    parsed = json.loads(result)
    assert parsed["_truncated"] is True
    assert "_raw_head" in parsed


# ─── _dedup_tool_results_by_md5 ────────────────────────────────────


def test_dedup_replaces_duplicates() -> None:
    body = "huge tool output " + "x" * 500
    msgs = [
        _user("hi"),
        _assistant_tool("t1", "read_file"),
        _tool_result("t1", body),
        _assistant_tool("t2", "read_file"),
        _tool_result("t2", body),  # duplicate
        _assistant_tool("t3", "read_file"),
        _tool_result("t3", body),  # also duplicate (most recent kept)
    ]
    deduped = _dedup_tool_results_by_md5(msgs)
    assert deduped == 2
    # Most recent (last) kept full body; older ones replaced.
    assert msgs[-1].results[0].content == body
    assert "dedup" in msgs[2].results[0].content
    assert "dedup" in msgs[4].results[0].content


def test_dedup_skips_short_bodies() -> None:
    msgs = [
        _assistant_tool("t1", "x"),
        _tool_result("t1", "tiny"),
        _assistant_tool("t2", "x"),
        _tool_result("t2", "tiny"),
    ]
    assert _dedup_tool_results_by_md5(msgs) == 0


# ─── _sanitize_tool_pairs ───────────────────────────────────────────


def test_sanitize_drops_orphan_tool_results() -> None:
    # tool_result for t99 has no matching assistant tool_use.
    msgs = [
        _user("hi"),
        _assistant_text("answer"),
        _tool_result("t99", "orphan output"),
    ]
    repaired = _sanitize_tool_pairs(msgs)
    assert repaired == 1
    assert all(not isinstance(m, ToolResultMessage) for m in msgs)


def test_sanitize_inserts_stub_for_missing_result() -> None:
    msgs: list = [
        _user("hi"),
        _assistant_tool("t42", "read_file"),
        _user("next"),
    ]
    repaired = _sanitize_tool_pairs(msgs)
    assert repaired == 1
    # A ToolResultMessage was inserted right after the assistant.
    assert isinstance(msgs[2], ToolResultMessage)
    assert msgs[2].results[0].tool_use_id == "t42"
    assert "missing" in msgs[2].results[0].content


def test_sanitize_no_op_when_paired() -> None:
    msgs = [
        _assistant_tool("t1", "read_file"),
        _tool_result("t1", "ok"),
    ]
    assert _sanitize_tool_pairs(msgs) == 0


# ─── _align_boundary_backward ───────────────────────────────────────


def test_align_boundary_backward_pulls_into_tool_group() -> None:
    msgs = [
        _user("u1"),
        _assistant_tool("t1", "read_file"),
        _tool_result("t1", "result"),
        _user("u2"),
    ]
    # cut_idx=3 would split the assistant + tool_result group.
    aligned = _align_boundary_backward(msgs, 3)
    assert aligned == 1


def test_align_boundary_backward_no_op_outside_group() -> None:
    msgs = [
        _user("u1"),
        _assistant_text("a1"),
        _user("u2"),
        _assistant_text("a2"),
    ]
    aligned = _align_boundary_backward(msgs, 2)
    assert aligned == 2


# ─── _ensure_last_user_message_in_tail ──────────────────────────────


def test_ensure_last_user_walks_back_when_user_in_prefix() -> None:
    msgs = [
        _user("old"),
        _assistant_text("a1"),
        _user("active task"),  # idx 2 — must end up in tail
        _assistant_text("a2"),
        _assistant_text("a3"),
    ]
    new_cut = _ensure_last_user_message_in_tail(msgs, 4)
    assert new_cut <= 2


def test_ensure_last_user_no_op_when_already_in_tail() -> None:
    msgs = [
        _user("old"),
        _assistant_text("a1"),
        _user("active task"),
    ]
    # cut_idx=2 already keeps the last user in tail.
    new_cut = _ensure_last_user_message_in_tail(msgs, 2)
    assert new_cut == 2


# ─── CompactionGuard ────────────────────────────────────────────────


def test_compaction_guard_no_skip_after_one_record() -> None:
    g = CompactionGuard()
    g.record(1000, 999)
    assert g.should_skip() is False


def test_compaction_guard_skips_after_two_ineffective() -> None:
    g = CompactionGuard()
    g.record(1000, 990)  # 1% saved
    g.record(1000, 995)  # 0.5% saved
    assert g.should_skip() is True


def test_compaction_guard_resets_after_effective() -> None:
    g = CompactionGuard()
    g.record(1000, 990)
    g.record(1000, 500)  # 50% saved — effective
    assert g.should_skip() is False


# ─── llm_structured_summarizer ──────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_structured_summarizer_calls_llm_and_prefixes() -> None:
    captured = {}

    async def stub_llm(prompt: str) -> str:
        captured["prompt"] = prompt
        return "## Active Task\nNone.\n## Goal\nTest.\n"

    msgs = [_user("hello"), _assistant_text("hi")]
    out = await llm_structured_summarizer(msgs, summarizer_llm=stub_llm)
    assert out.startswith(SUMMARY_PREFIX)
    assert "## Active Task" in out
    # Template included and serialised content present.
    assert "TURNS TO SUMMARIZE" in captured["prompt"]
    assert "[USER]" in captured["prompt"]


@pytest.mark.asyncio
async def test_llm_structured_summarizer_iterative_uses_prior() -> None:
    captured = {}

    async def stub_llm(prompt: str) -> str:
        captured["prompt"] = prompt
        return "updated body"

    msgs = [_user("new turn")]
    out = await llm_structured_summarizer(
        msgs, summarizer_llm=stub_llm, prior_summary="old summary body"
    )
    assert "PREVIOUS SUMMARY" in captured["prompt"]
    assert "old summary body" in captured["prompt"]
    assert "updated body" in out


# ─── structured_summarizer_pipeline (e2e) ───────────────────────────


@pytest.mark.asyncio
async def test_pipeline_e2e_with_stub_llm() -> None:
    async def stub_llm(prompt: str) -> str:
        return "summary body"

    msgs: list = []
    for i in range(8):
        msgs.append(_user(f"u{i} " + "x" * 400))
        msgs.append(_assistant_text(f"a{i} " + "y" * 400))

    new_msgs, prior = await structured_summarizer_pipeline(
        msgs, summarizer_llm=stub_llm, keep_tail_turns=4
    )
    # First message is now the summary SystemMessage.
    assert isinstance(new_msgs[0], SystemMessage)
    assert "summary body" in new_msgs[0].content
    assert prior is not None and "summary body" in prior
    # Tail preserved (keep_tail_turns=4 → last 4 messages).
    assert len(new_msgs) < len(msgs)
    # Last message of input must remain the last (real) message of output.
    last_input = msgs[-1]
    last_output = new_msgs[-1]
    assert getattr(last_output, "content", None) == getattr(last_input, "content", None)


@pytest.mark.asyncio
async def test_pipeline_skips_when_guard_says_so() -> None:
    async def stub_llm(prompt: str) -> str:
        raise AssertionError("LLM should not be called when guard skips")

    g = CompactionGuard()
    g.record(1000, 995)
    g.record(1000, 990)
    assert g.should_skip()

    msgs = [_user("hi"), _assistant_text("hello")]
    out, prior = await structured_summarizer_pipeline(
        msgs, summarizer_llm=stub_llm, guard=g, keep_tail_turns=2
    )
    assert prior is None
    assert len(out) == len(msgs)
