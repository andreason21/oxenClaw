"""Smart per-tool-result truncation."""

from __future__ import annotations

from oxenclaw.pi.messages import (
    TextContent,
    ToolResultBlock,
    ToolResultMessage,
)
from oxenclaw.pi.run.tool_result_truncation import (
    HARD_MAX_TOOL_RESULT_CHARS,
    MIN_KEEP_CHARS,
    calculate_max_tool_result_chars,
    session_likely_has_oversized_tool_results,
    truncate_oversized_tool_results_in_messages,
    truncate_tool_result_message,
    truncate_tool_result_text,
)


def test_passthrough_when_within_budget() -> None:
    text = "hello world"
    assert truncate_tool_result_text(text, 100) == text


def test_truncates_at_newline_boundary_in_head_only_mode() -> None:
    body = ("line\n" * 1000).rstrip()
    out = truncate_tool_result_text(body, 200)
    # Suffix appended.
    assert "Content truncated" in out
    # Cut at newline near budget — should not contain a partial fragment
    # like "lin" with no closing newline.
    pre_suffix = out.split("⚠️")[0]
    assert pre_suffix.endswith("\n") or pre_suffix.endswith("line")


def test_keeps_head_and_tail_when_tail_has_error_marker() -> None:
    head = "OK 1\nOK 2\nOK 3\n" * 1000
    tail = "Traceback (most recent call last):\n  File 'x'\nValueError: kapow\n"
    body = head + tail
    out = truncate_tool_result_text(body, 12_000)
    assert "Traceback" in out
    assert "ValueError: kapow" in out
    assert "middle content omitted" in out


def test_keeps_head_and_tail_when_tail_is_json_close() -> None:
    body = ("noise\n" * 5000) + '{"final": "summary"}'
    out = truncate_tool_result_text(body, 8_000)
    assert '"final": "summary"' in out
    assert "middle content omitted" in out


def test_calculate_budget_30pct_share() -> None:
    # 32K-token window → 30% × 32_000 × 4 chars/token = 38_400 chars
    assert calculate_max_tool_result_chars(32_000) == 38_400


def test_calculate_budget_capped_at_hard_max() -> None:
    # 2M-token tier — would compute to 2.4M chars, capped at 400K.
    assert calculate_max_tool_result_chars(2_000_000) == HARD_MAX_TOOL_RESULT_CHARS


def test_calculate_budget_floor_at_min_keep() -> None:
    # 1K window — 30% × 4 chars/token = 1_200 chars; floor lifts to MIN_KEEP_CHARS.
    assert calculate_max_tool_result_chars(1_000) == MIN_KEEP_CHARS


def test_truncate_message_with_string_content() -> None:
    big = "x" * 100_000
    block = ToolResultBlock(tool_use_id="t1", content=big, is_error=False)
    msg = ToolResultMessage(results=[block])
    n = truncate_tool_result_message(msg, max_chars=10_000)
    assert n == 1
    assert isinstance(msg.results[0].content, str)
    assert len(msg.results[0].content) <= 10_000 + 200  # allow suffix


def test_truncate_message_with_text_block_list() -> None:
    blocks = [
        TextContent(text="alpha " * 5000),
        TextContent(text="omega"),
    ]
    block = ToolResultBlock(tool_use_id="t1", content=blocks, is_error=False)
    msg = ToolResultMessage(results=[block])
    n = truncate_tool_result_message(msg, max_chars=4_000)
    assert n == 1
    new_content = msg.results[0].content
    assert isinstance(new_content, list)
    # The short "omega" block is well under MIN_KEEP_CHARS so survives untouched.
    assert any(isinstance(b, TextContent) and "omega" in b.text for b in new_content)


def test_session_pass_skips_when_under_budget() -> None:
    msgs = [
        ToolResultMessage(
            results=[ToolResultBlock(tool_use_id="t1", content="short text", is_error=False)]
        ),
    ]
    n = truncate_oversized_tool_results_in_messages(msgs, context_window_tokens=128_000)
    assert n == 0


def test_session_pass_trims_oversize_results() -> None:
    """A 200K-char tool result against a 32K-context model gets clipped."""
    msgs = [
        ToolResultMessage(
            results=[
                ToolResultBlock(
                    tool_use_id="t1",
                    content="x" * 200_000,
                    is_error=False,
                )
            ]
        ),
    ]
    n = truncate_oversized_tool_results_in_messages(msgs, context_window_tokens=32_000)
    assert n == 1
    content = msgs[0].results[0].content
    assert isinstance(content, str)
    # 32K-window cap: 30% × 32K × 4 = 38_400 chars — well below original.
    assert len(content) <= calculate_max_tool_result_chars(32_000) + 500


def test_session_pass_idempotent() -> None:
    msgs = [
        ToolResultMessage(
            results=[ToolResultBlock(tool_use_id="t1", content="x" * 200_000, is_error=False)]
        ),
    ]
    n1 = truncate_oversized_tool_results_in_messages(msgs, context_window_tokens=32_000)
    n2 = truncate_oversized_tool_results_in_messages(msgs, context_window_tokens=32_000)
    assert n1 == 1
    assert n2 == 0


def test_session_likely_has_oversized_returns_true_only_when_over() -> None:
    small = ToolResultMessage(
        results=[ToolResultBlock(tool_use_id="t1", content="hi", is_error=False)]
    )
    big = ToolResultMessage(
        results=[ToolResultBlock(tool_use_id="t2", content="x" * 200_000, is_error=False)]
    )
    assert session_likely_has_oversized_tool_results([small], context_window_tokens=32_000) is False
    assert (
        session_likely_has_oversized_tool_results([small, big], context_window_tokens=32_000)
        is True
    )
