"""Conversation history persistence.

Per (agent_id, session_key) JSON file at
`~/.oxenclaw/agents/<agent_id>/sessions/<session_key>.json`. Atomic write
via tmpfile + rename so a crash can't leave a corrupt history.

Port of openclaw's per-session transcript store.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class ConversationHistory:
    """Mutable list of Anthropic-format messages backed by a JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._messages: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._messages = []
            return
        messages = raw.get("messages") if isinstance(raw, dict) else None
        if isinstance(messages, list):
            self._messages = messages

    @property
    def path(self) -> Path:
        return self._path

    def messages(self) -> list[dict[str, Any]]:
        """Return a shallow copy so callers can't mutate internal state directly."""
        return list(self._messages)

    def append(self, message: dict[str, Any]) -> None:
        self._messages.append(message)

    def extend(self, messages: list[dict[str, Any]]) -> None:
        self._messages.extend(messages)

    def clear(self) -> None:
        self._messages = []

    def truncate_to_window(
        self,
        *,
        max_chars: int,
        preserve_system: bool = True,
    ) -> int:
        """Drop oldest non-system messages until total content fits `max_chars`.

        Sliding window keyed off serialized JSON length (cheap proxy for tokens
        ~ chars/4). Always keeps the leading system message if `preserve_system`.
        Drops in tool_call → tool_result pairs to avoid orphaning a tool result.
        Returns the number of messages removed.
        """
        if max_chars <= 0 or not self._messages:
            return 0
        head_offset = (
            1
            if preserve_system and self._messages and self._messages[0].get("role") == "system"
            else 0
        )

        def total() -> int:
            return sum(len(json.dumps(m, ensure_ascii=False)) for m in self._messages)

        removed = 0
        while total() > max_chars and len(self._messages) > head_offset + 1:
            drop_idx = head_offset
            # If the next message is a tool result, drop it together with the
            # preceding assistant tool_calls so the loop stays well-formed.
            while drop_idx < len(self._messages) and self._messages[drop_idx].get("role") == "tool":
                self._messages.pop(drop_idx)
                removed += 1
            if drop_idx < len(self._messages):
                self._messages.pop(drop_idx)
                removed += 1
        return removed

    def __len__(self) -> int:
        return len(self._messages)

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"messages": self._messages}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, self._path)
