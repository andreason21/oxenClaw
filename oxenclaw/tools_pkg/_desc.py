"""Hermes-style description composer for FunctionTool descriptions.

Models pick tools from the static description string baked into the
tool schema. Plain "what this does" prose isn't enough for small models
to route correctly — they reach for `web_search` when a `weather` tool
exists, hammer `shell` instead of `edit`, etc. Hermes (`hermes-agent`)
solved this by appending a stable `WHEN TO USE / WHEN NOT TO USE /
ALTERNATIVES` block to every tool description; we mirror the
convention here.

Keep it compact: the whole block must stay under a few hundred chars
so 30+ tools don't blow the schema-cache budget. We deliberately don't
mirror hermes's multi-paragraph blocks.
"""

from __future__ import annotations


def hermes_desc(
    summary: str,
    *,
    when_use: list[str] | tuple[str, ...] = (),
    when_skip: list[str] | tuple[str, ...] = (),
    alternatives: dict[str, str] | None = None,
    notes: str = "",
) -> str:
    """Compose a tool description in the hermes WHEN-TO-USE format.

    Args:
        summary: Existing one-paragraph "what this does" prose.
        when_use: Bullet phrases that complete "Use when …".
        when_skip: Bullet phrases that complete "Don't use when …".
        alternatives: Mapping of tool name → one-line "use it when".
        notes: Optional trailing IMPORTANT line (gotchas, approval, etc.).

    Returns:
        A single string with newline-separated sections. Empty sections
        are dropped so a tool with only `when_use` doesn't carry empty
        headers.
    """
    parts: list[str] = [summary.strip()]
    if when_use:
        parts.append("WHEN TO USE: " + "; ".join(s.strip() for s in when_use) + ".")
    if when_skip:
        parts.append(
            "WHEN NOT TO USE: " + "; ".join(s.strip() for s in when_skip) + "."
        )
    if alternatives:
        alt_lines = "; ".join(f"{name} ({why})" for name, why in alternatives.items())
        parts.append("ALTERNATIVES: " + alt_lines + ".")
    if notes:
        parts.append("IMPORTANT: " + notes.strip())
    return " ".join(parts)


__all__ = ["hermes_desc"]
