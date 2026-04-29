"""Pending-action recovery for short-affirmation user replies."""

from __future__ import annotations

from oxenclaw.agents.pending_action import (
    extract_unfulfilled_promise,
    looks_like_short_affirmation,
    render_pending_action_prelude,
)
from oxenclaw.pi import (
    AssistantMessage,
    TextContent,
    ToolUseBlock,
    UserMessage,
)

# ─── short-affirmation classifier ────────────────────────────────────


def test_short_affirmation_ko() -> None:
    for s in ["진행해", "진행해.", "진행해주세요", "계속", "계속해", "응", "네", "넵", "예", "오케이", "좋아"]:
        assert looks_like_short_affirmation(s), s


def test_short_affirmation_en() -> None:
    for s in ["yes", "Yes", "yeah", "yep", "ok", "OK", "okay", "sure", "go", "go ahead", "do it", "proceed", "continue"]:
        assert looks_like_short_affirmation(s), s


def test_short_affirmation_rejects_substantive_text() -> None:
    for s in [
        "진행해 그리고 X도 검색해줘",
        "yes please show me the file",
        "show me the weather",
        "오늘 수원 날씨 알려줘",
        "",
        "?",
    ]:
        assert not looks_like_short_affirmation(s), s


# ─── promise extraction ──────────────────────────────────────────────


def test_promise_extracted_when_no_tool_use() -> None:
    """The exact failing case from the user's transcript: assistant
    text says '확인하겠습니다' + JSON-in-text (no real tool_use)."""
    msgs = [
        UserMessage(content="weather tool 사용해"),
        AssistantMessage(
            content=[
                TextContent(
                    text="수원 날씨를 확인하겠습니다. ```json\n{\"tool\":\"weather\"}\n```"
                )
            ],
        ),
    ]
    snippet = extract_unfulfilled_promise(msgs)
    assert snippet is not None
    assert "확인하겠습니다" in snippet


def test_promise_returns_none_when_real_tool_use_exists() -> None:
    """If the assistant actually fired a tool, the runtime handled it
    — no pending action to recover."""
    msgs = [
        UserMessage(content="weather"),
        AssistantMessage(
            content=[
                TextContent(text="확인하겠습니다."),
                ToolUseBlock(id="t1", name="weather", input={"city": "Seoul"}),
            ],
        ),
    ]
    assert extract_unfulfilled_promise(msgs) is None


def test_promise_returns_none_when_no_promise_phrase() -> None:
    msgs = [
        UserMessage(content="hello"),
        AssistantMessage(content=[TextContent(text="hi! what do you need?")]),
    ]
    assert extract_unfulfilled_promise(msgs) is None


def test_promise_returns_none_with_empty_history() -> None:
    assert extract_unfulfilled_promise([]) is None


def test_promise_extraction_only_inspects_latest_assistant() -> None:
    """An older promise that was followed by a normal exchange should
    not retroactively trigger pending-action recovery."""
    msgs = [
        UserMessage(content="A"),
        AssistantMessage(content=[TextContent(text="확인하겠습니다.")]),
        UserMessage(content="B"),
        AssistantMessage(content=[TextContent(text="이전 답변과 무관한 새 답.")]),
    ]
    assert extract_unfulfilled_promise(msgs) is None


def test_en_promise_extracted() -> None:
    msgs = [
        UserMessage(content="weather"),
        AssistantMessage(
            content=[TextContent(text="Let me check the weather for you.")]
        ),
    ]
    snippet = extract_unfulfilled_promise(msgs)
    assert snippet is not None
    assert "check" in snippet.lower()


# ─── prelude rendering ───────────────────────────────────────────────


def test_render_pending_action_prelude_contains_directive() -> None:
    out = render_pending_action_prelude("수원 날씨를 확인하겠습니다.")
    assert "PENDING ACTION" in out
    assert "수원 날씨를 확인하겠습니다." in out
    assert "tool_use" in out  # tells the model what to emit
