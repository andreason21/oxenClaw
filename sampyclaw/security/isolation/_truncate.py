"""Helper used by every backend to safely cap captured stdout/stderr."""

from __future__ import annotations


def truncate(data: bytes, limit: int) -> tuple[str, bool]:
    """Decode bytes as UTF-8 (with replacement) and trim to ``limit`` bytes.

    Returns (decoded_text, was_truncated). Trims at byte level then decodes,
    so a multi-byte char on the boundary is handled by the replacement codec.
    """
    if limit <= 0 or len(data) <= limit:
        return data.decode("utf-8", errors="replace"), False
    head = data[:limit]
    trailer = b"\n...[truncated]"
    return (head + trailer).decode("utf-8", errors="replace"), True
