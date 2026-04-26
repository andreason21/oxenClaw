"""Per-thread reply-history buffer.

Mirrors openclaw `auto-reply/reply/history.ts` + the `plugin-sdk/reply-
history.ts` re-export surface. Use case: in group chats the bot needs
*recent* messages (whether or not it replied to them) so it can keep
context between its own replies. This is **separate from session
transcripts** — those track the agent's dialogue; this tracks the
chat's surrounding chatter.

Shape: `dict[key, list[HistoryEntry]]` where `key` is typically a
session/thread/chat id. Per-key list is bounded by `limit` (default 50);
the map itself is bounded by `MAX_HISTORY_KEYS` (1000) with LRU eviction
on the *outer* map (insertion-order based).

Markers `HISTORY_CONTEXT_MARKER` and `CURRENT_MESSAGE_MARKER` are the
exact strings openclaw uses, so prompts are byte-compatible across
ports.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

HISTORY_CONTEXT_MARKER = "[Chat messages since your last reply - for context]"
CURRENT_MESSAGE_MARKER = "[Current message]"
DEFAULT_GROUP_HISTORY_LIMIT = 50
MAX_HISTORY_KEYS = 1000


@dataclass(frozen=True)
class HistoryEntry:
    """One inbound message recorded for chat context."""

    sender: str
    body: str
    timestamp: float | None = None
    message_id: str | None = None


# ─── per-key list helpers ───────────────────────────────────────────


def record_pending_history_entry(
    history_map: dict[str, list[HistoryEntry]],
    key: str,
    entry: HistoryEntry,
    *,
    limit: int = DEFAULT_GROUP_HISTORY_LIMIT,
) -> None:
    """Append `entry` to `key`'s list, trimming oldest beyond `limit`."""
    bucket = history_map.setdefault(key, [])
    bucket.append(entry)
    if len(bucket) > limit:
        # Keep the most recent `limit` entries.
        del bucket[: len(bucket) - limit]


def record_pending_history_entry_if_enabled(
    history_map: dict[str, list[HistoryEntry]] | None,
    key: str,
    entry: HistoryEntry,
    *,
    limit: int = DEFAULT_GROUP_HISTORY_LIMIT,
) -> None:
    """Same as `record_pending_history_entry` but a None map is a no-op."""
    if history_map is None:
        return
    record_pending_history_entry(history_map, key, entry, limit=limit)


def clear_history_entries(history_map: dict[str, list[HistoryEntry]], key: str) -> None:
    """Drop the bucket for `key` entirely."""
    history_map.pop(key, None)


def clear_history_entries_if_enabled(
    history_map: dict[str, list[HistoryEntry]] | None, key: str
) -> None:
    if history_map is not None:
        clear_history_entries(history_map, key)


def evict_old_history_keys(
    history_map: dict[str, list[HistoryEntry]],
    *,
    max_keys: int = MAX_HISTORY_KEYS,
) -> int:
    """Bound the outer-map size via insertion-order LRU. Returns drop count."""
    if len(history_map) <= max_keys:
        return 0
    drop_count = len(history_map) - max_keys
    iterator = iter(list(history_map.keys()))
    dropped = 0
    for _ in range(drop_count):
        try:
            key = next(iterator)
        except StopIteration:
            break
        history_map.pop(key, None)
        dropped += 1
    return dropped


# ─── prompt assembly ────────────────────────────────────────────────


def format_entries(entries: Iterable[HistoryEntry], *, line_break: str = "\n") -> str:
    """Format entries as `sender: body` lines (openclaw-compatible)."""
    return line_break.join(f"{e.sender}: {e.body}" for e in entries)


def build_history_context(
    *,
    history_text: str,
    current_message: str,
    line_break: str = "\n",
) -> str:
    """Wrap pre-formatted history + current message with the standard markers.

    When `history_text` is empty/whitespace, returns `current_message`
    alone — matches openclaw behaviour to avoid leading-marker noise.
    """
    if not history_text.strip():
        return current_message
    return line_break.join(
        [
            HISTORY_CONTEXT_MARKER,
            history_text,
            "",
            CURRENT_MESSAGE_MARKER,
            current_message,
        ]
    )


def build_history_context_from_entries(
    *,
    entries: Iterable[HistoryEntry],
    current_message: str,
    line_break: str = "\n",
) -> str:
    return build_history_context(
        history_text=format_entries(entries, line_break=line_break),
        current_message=current_message,
        line_break=line_break,
    )


def build_history_context_from_map(
    *,
    history_map: Mapping[str, list[HistoryEntry]],
    key: str,
    current_message: str,
    line_break: str = "\n",
) -> str:
    """Look up `key` in `history_map` and render. Missing key → no context."""
    entries = history_map.get(key, [])
    return build_history_context_from_entries(
        entries=entries,
        current_message=current_message,
        line_break=line_break,
    )


def build_pending_history_context_from_map(
    *,
    history_map: Mapping[str, list[HistoryEntry]] | None,
    key: str,
    current_message: str,
    line_break: str = "\n",
) -> str:
    """Convenience: None-map → just the current message."""
    if history_map is None:
        return current_message
    return build_history_context_from_map(
        history_map=history_map,
        key=key,
        current_message=current_message,
        line_break=line_break,
    )


__all__ = [
    "CURRENT_MESSAGE_MARKER",
    "DEFAULT_GROUP_HISTORY_LIMIT",
    "HISTORY_CONTEXT_MARKER",
    "MAX_HISTORY_KEYS",
    "HistoryEntry",
    "build_history_context",
    "build_history_context_from_entries",
    "build_history_context_from_map",
    "build_pending_history_context_from_map",
    "clear_history_entries",
    "clear_history_entries_if_enabled",
    "evict_old_history_keys",
    "format_entries",
    "record_pending_history_entry",
    "record_pending_history_entry_if_enabled",
]
