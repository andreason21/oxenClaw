"""Tests for Telegram MarkdownV2/HTML escape + code block helpers."""

from __future__ import annotations

from sampyclaw.extensions.telegram.format import (
    MDV2_SPECIAL,
    code_block,
    escape_html,
    escape_markdown_v2,
    inline_code,
)


def test_mdv2_escapes_every_special_char() -> None:
    raw = "".join(MDV2_SPECIAL)
    out = escape_markdown_v2(raw)
    for ch in MDV2_SPECIAL:
        assert f"\\{ch}" in out


def test_mdv2_preserves_ordinary_text() -> None:
    assert escape_markdown_v2("hello world") == "hello world"


def test_mdv2_escapes_in_context() -> None:
    assert escape_markdown_v2("a.b-c") == "a\\.b\\-c"


def test_html_escape_amp_lt_gt() -> None:
    assert escape_html("<b>a & b</b>") == "&lt;b&gt;a &amp; b&lt;/b&gt;"


def test_html_escape_amp_first_avoids_double_escape() -> None:
    # Regression: naive ordering turns `<` into `&lt;` then `&` into `&amp;lt;`.
    assert escape_html("<") == "&lt;"
    assert escape_html("&") == "&amp;"


def test_code_block_without_language() -> None:
    assert code_block("a\nb") == "```\na\nb\n```"


def test_code_block_with_language() -> None:
    assert code_block("print(1)", "python") == "```python\nprint(1)\n```"


def test_inline_code_wraps_backticks() -> None:
    assert inline_code("x") == "`x`"


def test_inline_code_escapes_backtick() -> None:
    assert inline_code("a`b") == "`a\\`b`"
