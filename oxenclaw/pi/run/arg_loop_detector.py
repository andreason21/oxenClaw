"""Argument-level loop detection.

oxenClaw original (no direct openclaw upstream port). The
unknown-tool detector catches the case where the model spams a
non-existent name; this one catches the harder symptom where the
model calls the SAME registered tool with the SAME (or near-
identical) args repeatedly — e.g. `web_search(query="X")` returning
0 hits in a loop. openclaw's `tool-loop-detection.ts` has a
conceptually adjacent `genericRepeat` detector with a different
window/threshold shape; we did not port it 1:1.

The detector keeps a small fixed-size deque of recent
`(tool_name, args_digest)` pairs. When the most recent N entries are
all identical, the run loop aborts with a `loop_detection` stop so
the user sees a structured error rather than a silent stuck turn.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field


def _digest_args(args: dict | None) -> str:
    """Stable short hash of tool args.

    Uses canonical JSON (sorted keys) so identical-but-reordered args
    collapse to the same digest. Failures fall back to `str(args)`
    digest — pessimistic but never throws.
    """
    if not args:
        return "0"
    try:
        canon = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        canon = str(args)
    return hashlib.sha1(canon.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


@dataclass
class ArgLoopDetector:
    """Per-turn argument-loop tracker.

    `threshold` consecutive identical (name, digest) pairs trigger a
    stuck-loop signal. A single different call in the window resets
    the streak.
    """

    threshold: int = 4
    _streak: int = 0
    _last_key: tuple[str, str] | None = None
    _recent: deque[tuple[str, str]] = field(default_factory=lambda: deque(maxlen=16))

    def observe(self, tool_name: str, args: dict | None) -> bool:
        """Record one tool call. Returns True iff this completes a stuck streak."""
        key = (tool_name, _digest_args(args))
        self._recent.append(key)
        if key == self._last_key:
            self._streak += 1
        else:
            self._streak = 1
            self._last_key = key
        return self._streak >= self.threshold

    @property
    def streak(self) -> int:
        return self._streak

    @property
    def last_key(self) -> tuple[str, str] | None:
        return self._last_key

    def reset(self) -> None:
        self._streak = 0
        self._last_key = None
        self._recent.clear()


__all__ = ["ArgLoopDetector"]
