"""Reply chunking + dispatch primitives.

Port of openclaw `src/plugin-sdk/reply-runtime.ts` and `reply-dispatch-runtime.ts`.

Long replies get split into channel-compatible chunks (e.g., Telegram's 4096 char
limit). Chunks preserve code blocks and Markdown semantics where possible.
"""

from __future__ import annotations

from collections.abc import Iterator


def chunk_text(text: str, limit: int) -> Iterator[str]:
    """Split text into chunks no larger than `limit` bytes of UTF-8.

    Prefers to break at paragraph, then line, then word, then character boundaries.
    Mirrors openclaw's `splitForChannel` behaviour.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    remaining = text
    while len(remaining.encode("utf-8")) > limit:
        window = remaining[:limit]
        cut = _best_split(window)
        yield remaining[:cut]
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        yield remaining


def _best_split(window: str) -> int:
    for sep in ("\n\n", "\n", " "):
        idx = window.rfind(sep)
        if idx > 0:
            return idx + len(sep)
    return len(window)
