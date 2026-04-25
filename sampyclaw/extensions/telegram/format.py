"""Telegram text formatting helpers.

Port of openclaw `extensions/telegram/src/format.ts`. Provides the two parse
modes Telegram supports plus a `code_block` helper. Keep these pure — no I/O,
no aiogram imports.
"""

from __future__ import annotations

from typing import Final

# Per https://core.telegram.org/bots/api#markdownv2-style — every one of these
# must be backslash-escaped outside of formatting entities.
MDV2_SPECIAL: Final[frozenset[str]] = frozenset(
    "_*[]()~`>#+-=|{}.!\\"
)


def escape_markdown_v2(text: str) -> str:
    """Backslash-escape every MarkdownV2 special char."""
    return "".join(f"\\{ch}" if ch in MDV2_SPECIAL else ch for ch in text)


def escape_html(text: str) -> str:
    """Minimal HTML escape for Telegram parse_mode=HTML."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def code_block(body: str, language: str | None = None) -> str:
    """Fence `body` as a MarkdownV2 code block. The fences themselves don't need escaping."""
    header = f"```{language}" if language else "```"
    return f"{header}\n{body}\n```"


def inline_code(body: str) -> str:
    """MarkdownV2 inline code span. Only backticks inside need escaping."""
    return f"`{body.replace('`', '\\`')}`"
