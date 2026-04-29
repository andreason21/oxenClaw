"""Pseudo-tool extraction from assistant reply text."""

from __future__ import annotations

from oxenclaw.agents.pseudo_tool import extract_pseudo_tool_call

KNOWN = {"weather", "web_search", "memory_save"}


def _is_known(name: str) -> bool:
    return name in KNOWN


def test_returns_none_for_empty_text() -> None:
    assert extract_pseudo_tool_call("", is_known_tool=_is_known) is None
    assert extract_pseudo_tool_call("   ", is_known_tool=_is_known) is None


def test_returns_none_when_no_json_present() -> None:
    text = "수원 날씨를 확인하겠습니다."
    assert extract_pseudo_tool_call(text, is_known_tool=_is_known) is None


def test_extracts_fenced_json_with_flat_args() -> None:
    """The actual failing transcript shape: fenced ```json``` block
    with `{"tool": "weather", "location": "...", "query": "..."}`."""
    text = (
        "수원 날씨를 확인해드리겠습니다.\n\n"
        "```json\n"
        '{"tool": "weather", "location": "Suwon, South Korea", '
        '"query": "weather"}\n'
        "```\n"
        "수원 지역의 현재 날씨 정보를 확인하겠습니다."
    )
    pseudo = extract_pseudo_tool_call(text, is_known_tool=_is_known)
    assert pseudo is not None
    assert pseudo.name == "weather"
    assert pseudo.args == {"location": "Suwon, South Korea", "query": "weather"}


def test_unfenced_json_object_in_text() -> None:
    """Bare top-level JSON without code fence — also valid."""
    text = 'I will check: {"tool": "web_search", "query": "ai news"} now.'
    pseudo = extract_pseudo_tool_call(text, is_known_tool=_is_known)
    assert pseudo is not None
    assert pseudo.name == "web_search"
    assert pseudo.args == {"query": "ai news"}


def test_openai_style_function_wrapping() -> None:
    """Some models emit `{"function": {"name": ..., "arguments": {...}}}`."""
    text = (
        "```json\n"
        '{"function": {"name": "weather", "arguments": {"city": "Seoul"}}}\n'
        "```"
    )
    pseudo = extract_pseudo_tool_call(text, is_known_tool=_is_known)
    assert pseudo is not None
    assert pseudo.name == "weather"
    assert pseudo.args == {"city": "Seoul"}


def test_name_with_input_key() -> None:
    text = '```json\n{"name": "weather", "input": {"city": "Suwon"}}\n```'
    pseudo = extract_pseudo_tool_call(text, is_known_tool=_is_known)
    assert pseudo is not None
    assert pseudo.name == "weather"
    assert pseudo.args == {"city": "Suwon"}


def test_arguments_as_stringified_json() -> None:
    """OpenAI streaming dumps `arguments` as a JSON string."""
    text = (
        '```json\n{"name": "web_search", "arguments": "{\\"query\\": "kimchi"}"}\n```'
    )
    # The escaped string isn't valid JSON in our test (odd quotes); use a
    # cleaner version:
    text = '```json\n{"name": "web_search", "arguments": "{\\"query\\": \\"kimchi\\"}"}\n```'
    pseudo = extract_pseudo_tool_call(text, is_known_tool=_is_known)
    assert pseudo is not None
    assert pseudo.name == "web_search"
    assert pseudo.args == {"query": "kimchi"}


def test_skips_unknown_tool_names() -> None:
    """Documentation snippets that mention non-registered tools must
    not be auto-fired (false positive risk)."""
    text = '```json\n{"tool": "imaginary_tool", "x": 1}\n```'
    assert extract_pseudo_tool_call(text, is_known_tool=_is_known) is None


def test_picks_first_recognised_call_when_multiple_blocks() -> None:
    text = (
        "First: ```json\n"
        '{"tool": "imaginary", "x": 1}\n'
        "```\n"
        "Then the real one: ```json\n"
        '{"tool": "weather", "city": "Seoul"}\n'
        "```"
    )
    pseudo = extract_pseudo_tool_call(text, is_known_tool=_is_known)
    assert pseudo is not None
    assert pseudo.name == "weather"


def test_invalid_json_is_silently_skipped() -> None:
    text = "```json\n{tool: weather, broken}\n```"
    assert extract_pseudo_tool_call(text, is_known_tool=_is_known) is None
