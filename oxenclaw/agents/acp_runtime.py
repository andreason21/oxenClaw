"""ACP runtime track — Protocol surface for real bidirectional ACP.

Stub. Mirrors the openclaw `AcpRuntime` interface defined in
`src/acp/runtime/types.ts:118-152`. Backends (codex/claude/gemini
ACP servers, or in-process fakes for tests) will implement this
Protocol and register themselves with a future
`oxenclaw.acp.runtime.registry` module.

Today, no concrete backend implements the full surface — the
`acp_subprocess.spawn_acp` one-shot path is the only working dispatch
mode. This file pins the *shape* so subsequent commits (NDJSON
framing, manager singleton, parent-stream relay) can land against a
stable Python contract instead of moving targets.

Type names follow the openclaw spelling 1:1 to keep the porting
diff readable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

AcpRuntimePromptMode = Literal["prompt", "steer"]
AcpRuntimeSessionMode = Literal["persistent", "oneshot"]
AcpRuntimeControl = Literal[
    "session/set_mode",
    "session/set_config_option",
    "session/status",
]

# Open string set: official tags + backend-specific extensions are both
# valid. Mirrors the `(string & {})` escape hatch in openclaw.
AcpSessionUpdateTag = str

OFFICIAL_SESSION_UPDATE_TAGS: frozenset[str] = frozenset(
    {
        "agent_message_chunk",
        "agent_thought_chunk",
        "tool_call",
        "tool_call_update",
        "usage_update",
        "available_commands_update",
        "current_mode_update",
        "config_option_update",
        "session_info_update",
        "plan",
    }
)


@dataclass(frozen=True)
class AcpRuntimeHandle:
    """Stable identifier for an ACP session held by a backend.

    `session_key` is the oxenclaw-side key (matches gateway session
    storage). `runtime_session_name` is the backend's user-facing
    label. The optional fields surface the backend's internal
    identifiers when the adapter exposes them (e.g. acpx record id).
    """

    session_key: str
    backend: str
    runtime_session_name: str
    cwd: str | None = None
    acpx_record_id: str | None = None
    backend_session_id: str | None = None
    agent_session_id: str | None = None


@dataclass(frozen=True)
class AcpRuntimeEnsureInput:
    session_key: str
    agent: str
    mode: AcpRuntimeSessionMode
    resume_session_id: str | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None


@dataclass(frozen=True)
class AcpRuntimeTurnAttachment:
    media_type: str
    data: str


@dataclass(frozen=True)
class AcpRuntimeTurnInput:
    handle: AcpRuntimeHandle
    text: str
    mode: AcpRuntimePromptMode
    request_id: str
    attachments: list[AcpRuntimeTurnAttachment] = field(default_factory=list)


@dataclass(frozen=True)
class AcpRuntimeCapabilities:
    controls: list[AcpRuntimeControl] = field(default_factory=list)
    config_option_keys: list[str] | None = None


@dataclass(frozen=True)
class AcpRuntimeStatus:
    summary: str | None = None
    acpx_record_id: str | None = None
    backend_session_id: str | None = None
    agent_session_id: str | None = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class AcpRuntimeDoctorReport:
    ok: bool
    message: str
    code: str | None = None
    install_command: str | None = None
    details: list[str] = field(default_factory=list)


# Event union — matches openclaw runtime/types.ts:85-116.
@dataclass(frozen=True)
class AcpEventTextDelta:
    text: str
    stream: Literal["output", "thought"] | None = None
    tag: AcpSessionUpdateTag | None = None
    type: Literal["text_delta"] = "text_delta"


@dataclass(frozen=True)
class AcpEventStatus:
    text: str
    tag: AcpSessionUpdateTag | None = None
    used: int | None = None
    size: int | None = None
    type: Literal["status"] = "status"


@dataclass(frozen=True)
class AcpEventToolCall:
    text: str
    tag: AcpSessionUpdateTag | None = None
    tool_call_id: str | None = None
    status: str | None = None
    title: str | None = None
    type: Literal["tool_call"] = "tool_call"


@dataclass(frozen=True)
class AcpEventDone:
    stop_reason: str | None = None
    type: Literal["done"] = "done"


@dataclass(frozen=True)
class AcpEventError:
    message: str
    code: str | None = None
    retryable: bool | None = None
    type: Literal["error"] = "error"


AcpRuntimeEvent = (
    AcpEventTextDelta
    | AcpEventStatus
    | AcpEventToolCall
    | AcpEventDone
    | AcpEventError
)


@runtime_checkable
class AcpRuntime(Protocol):
    """Protocol every ACP backend must satisfy.

    Required methods only. The openclaw interface marks several
    methods as optional (`?:` in TS); in Python those live on
    `AcpRuntimeOptional` and call sites should test for them with
    `hasattr` or a `runtime_checkable` `isinstance` check.

    Stub — no concrete implementation exists yet. This file pins the
    contract so `acp/framing.py`, `acp/protocol.py`, and the future
    `AcpSessionManager` can target a stable shape.
    """

    async def ensure_session(
        self, input: AcpRuntimeEnsureInput
    ) -> AcpRuntimeHandle: ...

    def run_turn(
        self, input: AcpRuntimeTurnInput
    ) -> AsyncIterator[AcpRuntimeEvent]: ...

    async def cancel(
        self, *, handle: AcpRuntimeHandle, reason: str | None = None
    ) -> None: ...

    async def close(
        self,
        *,
        handle: AcpRuntimeHandle,
        reason: str,
        discard_persistent_state: bool = False,
    ) -> None: ...


@runtime_checkable
class AcpRuntimeOptional(Protocol):
    """Optional capability surface — match openclaw's `?:` methods.

    Backends opt in to these by defining the methods. Call sites
    must check first (`isinstance(rt, AcpRuntimeOptional)` for the
    full set, or `hasattr(rt, "set_mode")` for one method).
    """

    async def get_capabilities(
        self, *, handle: AcpRuntimeHandle | None = None
    ) -> AcpRuntimeCapabilities: ...

    async def get_status(
        self, *, handle: AcpRuntimeHandle
    ) -> AcpRuntimeStatus: ...

    async def set_mode(
        self, *, handle: AcpRuntimeHandle, mode: str
    ) -> None: ...

    async def set_config_option(
        self, *, handle: AcpRuntimeHandle, key: str, value: str
    ) -> None: ...

    async def doctor(self) -> AcpRuntimeDoctorReport: ...

    async def prepare_fresh_session(self, *, session_key: str) -> None: ...


__all__ = [
    "AcpEventDone",
    "AcpEventError",
    "AcpEventStatus",
    "AcpEventTextDelta",
    "AcpEventToolCall",
    "AcpRuntime",
    "AcpRuntimeCapabilities",
    "AcpRuntimeControl",
    "AcpRuntimeDoctorReport",
    "AcpRuntimeEnsureInput",
    "AcpRuntimeEvent",
    "AcpRuntimeHandle",
    "AcpRuntimeOptional",
    "AcpRuntimePromptMode",
    "AcpRuntimeSessionMode",
    "AcpRuntimeStatus",
    "AcpRuntimeTurnAttachment",
    "AcpRuntimeTurnInput",
    "AcpSessionUpdateTag",
    "OFFICIAL_SESSION_UPDATE_TAGS",
]
