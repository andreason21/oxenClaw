"""Pending-action recovery for short-affirmation user replies.

When a user says "진행해" / "yes" / "ok" right after the model
*promised* to do something but never actually fired the tool, the
model on the next turn often just produces another disconnected
reply because nothing in the transcript carries the action forward.

This module supplies two cheap heuristics:

- `looks_like_short_affirmation(text)` — KO + EN one-shot affirmations.
- `extract_unfulfilled_promise(messages)` — when the most recent
  assistant message contains a "I'll check / 확인하겠습니다 / let me
  look up …" promise but no `ToolUseBlock`, return that promise text
  so the caller can prepend a nudge to the next user message.

The pseudo-tool auto-fire path (`oxenclaw.agents.pseudo_tool`) covers
the case where the model wrote the call as JSON in text. This module
covers the residual case where the model only narrated an intent
without any JSON to parse.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

# Roughly: greetings/yes/ok in KO + EN, with optional punctuation.
# Kept very tight so we don't trigger on substantive replies that
# happen to be short.
_AFFIRM_RE = re.compile(
    r"^\s*(?:"
    r"진행(?:해|해줘|하자|해주세요)?|"
    r"계속(?:해|해줘|해주세요)?|"
    r"해|해줘|해주세요|"
    r"네|넵|예|응|어|오케이|좋아|좋아요|"
    r"yes|yeah|yep|sure|ok|okay|please|"
    r"go(?:\s*ahead)?|do\s*it|proceed|continue"
    r")\s*[.!?]*\s*$",
    re.IGNORECASE,
)


# Phrases the model uses to signal "I am about to do X". Both KO and
# EN variants because mixed-language replies are common.
_PROMISE_RE = re.compile(
    r"(?:"
    r"확인(?:하겠습니다|할게요|할게|해드리겠습니다|해드릴게요|해드릴게)|"
    r"조회(?:하겠습니다|할게요|할게|해드리겠습니다)|"
    r"검색(?:하겠습니다|할게요|할게|해드리겠습니다)|"
    r"찾아(?:보겠습니다|볼게요|볼게|드리겠습니다)|"
    r"가져(?:오겠습니다|올게요|올게)|"
    r"실행(?:하겠습니다|할게요|할게|해드리겠습니다)|"
    r"불러(?:오겠습니다|올게요|올게)|"
    r"알아(?:보겠습니다|볼게요|볼게|봐드릴게요)|"
    r"let\s*me\s*(?:check|look\s*up|search|fetch|fire|run|call|grab)|"
    r"i'?ll\s*(?:check|look\s*up|search|fetch|fire|run|call|grab)|"
    r"i\s*am\s*going\s*to\s*(?:check|look\s*up|search|fetch|run|call)|"
    r"checking\s*(?:the|your)?\s*\w+|"
    r"about\s*to\s*(?:check|run|call|fetch)"
    r")",
    re.IGNORECASE,
)


def looks_like_short_affirmation(text: str) -> bool:
    """True when `text` is a one-shot 'go ahead' style reply that has
    no information of its own — the prior assistant turn supplies the
    intent."""
    if not text:
        return False
    return bool(_AFFIRM_RE.match(text.strip()))


def extract_unfulfilled_promise(messages: Iterable[Any]) -> str | None:
    """Walk `messages` newest-to-oldest until the first assistant turn.

    Returns the matching promise phrase (one short snippet) when:
      * the most recent assistant turn contains a promise marker, AND
      * that turn has no `ToolUseBlock` (i.e. the model talked about
        firing a tool but never actually did).

    Returns None otherwise — including when the most recent assistant
    turn already carried a real tool call (the runtime handled it).
    """
    last_assistant = _find_latest_assistant(messages)
    if last_assistant is None:
        return None
    text, has_tool_use = last_assistant
    if has_tool_use:
        return None
    m = _PROMISE_RE.search(text)
    if not m:
        return None
    return _surrounding_sentence(text, m.start(), m.end())


def _find_latest_assistant(messages: Iterable[Any]) -> tuple[str, bool] | None:
    """Return (text, has_tool_use) for the most recent assistant
    message, or None when there isn't one."""
    materialised = list(messages)
    for msg in reversed(materialised):
        # Duck-type to avoid an import cycle with pi.messages.
        role = getattr(msg, "role", None)
        if role != "assistant":
            continue
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            return None
        text_parts: list[str] = []
        has_tool = False
        for block in content:
            btype = getattr(block, "type", None)
            if btype == "text":
                t = getattr(block, "text", "")
                if t:
                    text_parts.append(t)
            elif btype == "tool_use":
                has_tool = True
        return ("\n".join(text_parts), has_tool)
    return None


def _surrounding_sentence(text: str, start: int, end: int) -> str:
    """Return up to 200 chars of context centered on the match —
    enough for the model to recognise its own promise without
    flooding the prelude."""
    left = max(0, start - 80)
    right = min(len(text), end + 80)
    snippet = text[left:right].strip()
    if len(snippet) > 200:
        snippet = snippet[:200] + "…"
    return snippet


def render_pending_action_prelude(promise_snippet: str) -> str:
    """Tight prelude prepended to the user's affirmation message.

    User-side (not system-side) for the same reason as
    `format_memories_as_prelude`: small local models attend much more
    strongly to user-message context than to system blocks.
    """
    return (
        "[PENDING ACTION] Your previous reply promised to do something but "
        "never actually called a tool — the user just said 'proceed'. "
        "Re-read the snippet below from your last reply, identify which "
        "tool you were going to call, and CALL IT NOW with a real "
        "tool_use block (do not write the call as JSON in your reply "
        "text). If a required argument is missing, use the recalled-"
        "memories block above for any user location/context.\n"
        f'  Last promise: "{promise_snippet}"'
    )


__all__ = [
    "extract_unfulfilled_promise",
    "looks_like_short_affirmation",
    "render_pending_action_prelude",
]
