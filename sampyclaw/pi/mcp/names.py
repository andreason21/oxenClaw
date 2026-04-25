"""Tool / server name sanitization + collision dedup.

Mirrors openclaw `src/agents/pi-bundle-mcp-names.ts`. MCP server names and
tool names need to be coerced into a `[A-Za-z0-9_-]` shape that LLM
providers accept as identifiers, and the combined `<server>__<tool>`
identifier capped at 64 chars (Anthropic / OpenAI limits).
"""

from __future__ import annotations

import re

_SAFE_CHAR_RE = re.compile(r"[^A-Za-z0-9_-]")
TOOL_NAME_SEPARATOR = "__"
TOOL_NAME_MAX_PREFIX = 30
TOOL_NAME_MAX_TOTAL = 64


def _sanitize_fragment(raw: str, fallback: str, max_chars: int | None = None) -> str:
    cleaned = _SAFE_CHAR_RE.sub("-", raw.strip())
    normalized = cleaned or fallback
    if max_chars is None:
        return normalized
    return normalized[:max_chars] if len(normalized) > max_chars else normalized


def sanitize_server_name(raw: str, used_names: set[str]) -> str:
    """Return a unique provider-safe server name.

    The returned name is registered in `used_names` (mutated) for collision
    avoidance with future calls.
    """
    base = _sanitize_fragment(raw, "mcp", TOOL_NAME_MAX_PREFIX)
    candidate = base
    n = 2
    while candidate.lower() in used_names:
        suffix = f"-{n}"
        head_len = max(1, TOOL_NAME_MAX_PREFIX - len(suffix))
        candidate = f"{base[:head_len]}{suffix}"
        n += 1
    used_names.add(candidate.lower())
    return candidate


def sanitize_tool_name(raw: str) -> str:
    return _sanitize_fragment(raw, "tool")


def normalize_reserved_names(names: list[str] | tuple[str, ...] | None) -> set[str]:
    """Lower-case + filter empties — matches openclaw normalization."""
    if not names:
        return set()
    return {n.strip().lower() for n in names if isinstance(n, str) and n.strip()}


def build_safe_tool_name(
    *,
    server_name: str,
    tool_name: str,
    reserved_names: set[str],
) -> str:
    """Produce a `<server>__<tool>` name that fits the 64-char total cap.

    Truncates the tool segment first (server segment is already capped to 30).
    Disambiguates by appending `-N` to the tool segment if a collision exists
    in `reserved_names`. Mutation-free with respect to `reserved_names`.
    """
    cleaned_tool = sanitize_tool_name(tool_name)
    max_tool_chars = max(
        1, TOOL_NAME_MAX_TOTAL - len(server_name) - len(TOOL_NAME_SEPARATOR)
    )
    truncated_tool = cleaned_tool[:max_tool_chars] or "tool"
    candidate_tool = truncated_tool
    candidate = f"{server_name}{TOOL_NAME_SEPARATOR}{candidate_tool}"
    n = 2
    while candidate.lower() in reserved_names:
        suffix = f"-{n}"
        head = (truncated_tool or "tool")[
            : max(1, max_tool_chars - len(suffix))
        ]
        candidate_tool = f"{head}{suffix}"
        candidate = f"{server_name}{TOOL_NAME_SEPARATOR}{candidate_tool}"
        n += 1
    return candidate
