"""Session policy: chat type, send policy, model/thinking overrides, provenance.

Mirrors:
- `openclaw/src/sessions/session-chat-type.ts` (+ shared)
- `openclaw/src/sessions/send-policy.ts`
- `openclaw/src/sessions/model-overrides.ts` + `level-overrides.ts`
- `openclaw/src/sessions/input-provenance.ts`
- `openclaw/src/sessions/session-key-utils.ts`

These are the metadata + behavior knobs that ride alongside an
`AgentSession` to control *how* a turn is processed, separate from the
model/transcript itself. Stored under `session.metadata["policy"]` so
they round-trip through the existing SessionManager unchanged.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sampyclaw.pi.session import AgentSession
from sampyclaw.pi.thinking import ThinkingLevel


# ─── Chat type ───────────────────────────────────────────────────────


class SessionChatType(str, Enum):
    """The *kind* of conversation context.

    - `dm`: a direct one-on-one with the user.
    - `group`: a multi-participant room; the agent shouldn't reply unless
      addressed (handled by SendPolicy).
    - `thread`: a topic thread inside a group/channel; the agent treats
      the thread as a sub-conversation with isolated history.
    - `broadcast`: outbound-only (e.g. cron-driven announcements); inbound
      messages are not awaited.
    """

    DM = "dm"
    GROUP = "group"
    THREAD = "thread"
    BROADCAST = "broadcast"


# ─── Send policy ─────────────────────────────────────────────────────


class SendMode(str, Enum):
    """Decides whether the agent should reply to a given inbound."""

    DM_ONLY = "dm_only"  # Reply only in DMs; ignore groups.
    ADDRESSED_ONLY = "addressed_only"  # Reply only when @mentioned / replied-to.
    PAIRING = "pairing"  # Allow once a pairing handshake completed.
    ALLOWLIST = "allowlist"  # Reply only when sender_id is on `allow`.
    OPEN = "open"  # Reply to everyone (use with care).


@dataclass(frozen=True)
class SendPolicy:
    """Combined send-side policy."""

    mode: SendMode = SendMode.DM_ONLY
    allow: tuple[str, ...] = ()  # sender_ids
    deny: tuple[str, ...] = ()
    pairing_completed: bool = False
    addressed_handles: tuple[str, ...] = ()  # bot @mentions to recognise

    def should_reply(
        self,
        *,
        chat_type: SessionChatType,
        sender_id: str,
        text: str,
        is_reply_to_bot: bool = False,
    ) -> bool:
        """Apply the policy. Returns True iff the agent should respond."""
        if sender_id in self.deny:
            return False
        if self.mode is SendMode.DM_ONLY:
            return chat_type is SessionChatType.DM
        if self.mode is SendMode.ALLOWLIST:
            return sender_id in self.allow
        if self.mode is SendMode.PAIRING:
            return self.pairing_completed
        if self.mode is SendMode.ADDRESSED_ONLY:
            if is_reply_to_bot:
                return True
            lowered = text.lower()
            return any(h.lower() in lowered for h in self.addressed_handles)
        # OPEN: only blocked by deny.
        return True


# ─── Model + thinking overrides ─────────────────────────────────────


@dataclass(frozen=True)
class SessionOverrides:
    """Per-session overrides applied on top of agent defaults.

    The PiAgent reads these from `session.metadata['policy']` before
    building the per-turn RuntimeConfig.
    """

    model_id: str | None = None
    thinking: ThinkingLevel | str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    extra_params: dict[str, Any] = field(default_factory=dict)


# ─── Input provenance ────────────────────────────────────────────────


@dataclass(frozen=True)
class InputProvenance:
    """Where did the inbound message originate?

    Tracked per turn to enable: per-channel rate limiting, audit logs,
    "agent forwarded its own message" loop detection, and reply-to
    reconstruction across channels.
    """

    channel: str
    account_id: str
    chat_id: str
    sender_id: str
    thread_id: str | None = None
    message_id: str | None = None
    received_at: float = 0.0


# ─── Session key utilities ──────────────────────────────────────────


_KEY_SAFE_RE = re.compile(r"[^a-zA-Z0-9._:-]+")


def normalize_session_key(raw: str, *, max_len: int = 200) -> str:
    """Produce a filesystem-safe, dispatch-stable session key.

    - Lowercases.
    - Replaces unsafe characters with `_`.
    - Truncates to `max_len`; long keys get a sha1 suffix so collisions
      across truncation are vanishingly rare.
    """
    if not raw:
        return "anon"
    lowered = raw.strip().lower()
    safe = _KEY_SAFE_RE.sub("_", lowered)
    if len(safe) <= max_len:
        return safe
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    head = safe[: max_len - 9]
    return f"{head}.{digest}"


def derive_session_key(
    *, channel: str, account_id: str, chat_id: str, thread_id: str | None = None
) -> str:
    """Deterministic key from envelope address. Mirrors
    `session-key-utils.ts` `deriveSessionKey`."""
    parts = [channel, account_id, chat_id]
    if thread_id:
        parts.append(f"t:{thread_id}")
    return normalize_session_key(":".join(parts))


# ─── Read/write to session.metadata ──────────────────────────────────


_POLICY_KEY = "policy"


def _coerce_chat_type(v: Any) -> SessionChatType:
    if isinstance(v, SessionChatType):
        return v
    try:
        return SessionChatType(v)
    except (ValueError, TypeError):
        return SessionChatType.DM


def _coerce_send_mode(v: Any) -> SendMode:
    if isinstance(v, SendMode):
        return v
    try:
        return SendMode(v)
    except (ValueError, TypeError):
        return SendMode.DM_ONLY


@dataclass(frozen=True)
class SessionPolicy:
    """Bundle attached to one session via metadata."""

    chat_type: SessionChatType = SessionChatType.DM
    send: SendPolicy = field(default_factory=SendPolicy)
    overrides: SessionOverrides = field(default_factory=SessionOverrides)
    provenance: InputProvenance | None = None


def serialize_policy(p: SessionPolicy) -> dict[str, Any]:
    return {
        "chat_type": p.chat_type.value,
        "send": {
            "mode": p.send.mode.value,
            "allow": list(p.send.allow),
            "deny": list(p.send.deny),
            "pairing_completed": p.send.pairing_completed,
            "addressed_handles": list(p.send.addressed_handles),
        },
        "overrides": {
            "model_id": p.overrides.model_id,
            "thinking": (
                p.overrides.thinking.value
                if isinstance(p.overrides.thinking, ThinkingLevel)
                else p.overrides.thinking
            ),
            "temperature": p.overrides.temperature,
            "max_tokens": p.overrides.max_tokens,
            "extra_params": dict(p.overrides.extra_params),
        },
        "provenance": (
            {
                "channel": p.provenance.channel,
                "account_id": p.provenance.account_id,
                "chat_id": p.provenance.chat_id,
                "sender_id": p.provenance.sender_id,
                "thread_id": p.provenance.thread_id,
                "message_id": p.provenance.message_id,
                "received_at": p.provenance.received_at,
            }
            if p.provenance is not None
            else None
        ),
    }


def deserialize_policy(raw: dict[str, Any] | None) -> SessionPolicy:
    if not isinstance(raw, dict):
        return SessionPolicy()
    send_raw = raw.get("send") or {}
    over_raw = raw.get("overrides") or {}
    prov_raw = raw.get("provenance")
    send = SendPolicy(
        mode=_coerce_send_mode(send_raw.get("mode", "dm_only")),
        allow=tuple(send_raw.get("allow") or ()),
        deny=tuple(send_raw.get("deny") or ()),
        pairing_completed=bool(send_raw.get("pairing_completed", False)),
        addressed_handles=tuple(send_raw.get("addressed_handles") or ()),
    )
    overrides = SessionOverrides(
        model_id=over_raw.get("model_id"),
        thinking=over_raw.get("thinking"),
        temperature=over_raw.get("temperature"),
        max_tokens=over_raw.get("max_tokens"),
        extra_params=dict(over_raw.get("extra_params") or {}),
    )
    provenance = None
    if isinstance(prov_raw, dict):
        provenance = InputProvenance(
            channel=prov_raw.get("channel", ""),
            account_id=prov_raw.get("account_id", ""),
            chat_id=prov_raw.get("chat_id", ""),
            sender_id=prov_raw.get("sender_id", ""),
            thread_id=prov_raw.get("thread_id"),
            message_id=prov_raw.get("message_id"),
            received_at=float(prov_raw.get("received_at", 0.0)),
        )
    return SessionPolicy(
        chat_type=_coerce_chat_type(raw.get("chat_type", "dm")),
        send=send,
        overrides=overrides,
        provenance=provenance,
    )


def get_policy(session: AgentSession) -> SessionPolicy:
    return deserialize_policy(session.metadata.get(_POLICY_KEY))


def set_policy(session: AgentSession, policy: SessionPolicy) -> None:
    session.metadata = {**session.metadata, _POLICY_KEY: serialize_policy(policy)}


__all__ = [
    "InputProvenance",
    "SendMode",
    "SendPolicy",
    "SessionChatType",
    "SessionOverrides",
    "SessionPolicy",
    "derive_session_key",
    "deserialize_policy",
    "get_policy",
    "normalize_session_key",
    "serialize_policy",
    "set_policy",
]
