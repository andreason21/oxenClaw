"""Shape test for the AcpRuntime Protocol stub.

These tests verify the Protocol contract is well-formed Python and
that a minimal fake conforming to the required methods is recognised
by `runtime_checkable`. The tests do NOT exercise any wire protocol —
that arrives in the framing/protocol commit. They only pin the
shape so future commits cannot drift the surface unnoticed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from oxenclaw.agents.acp_runtime import (
    OFFICIAL_SESSION_UPDATE_TAGS,
    AcpEventDone,
    AcpEventError,
    AcpEventTextDelta,
    AcpRuntime,
    AcpRuntimeEnsureInput,
    AcpRuntimeEvent,
    AcpRuntimeHandle,
    AcpRuntimeOptional,
    AcpRuntimeTurnInput,
)


class _MinimalFakeRuntime:
    """Implements only the required `AcpRuntime` methods.

    Optional methods (get_capabilities, get_status, set_mode,
    set_config_option, doctor, prepare_fresh_session) are omitted
    on purpose — runtime_checkable only enforces presence of the
    methods, not full subset matching, so this is mainly a
    structural sanity check.
    """

    async def ensure_session(
        self, input: AcpRuntimeEnsureInput
    ) -> AcpRuntimeHandle:
        return AcpRuntimeHandle(
            session_key=input.session_key,
            backend="fake",
            runtime_session_name="fake-session",
        )

    def run_turn(
        self, input: AcpRuntimeTurnInput
    ) -> AsyncIterator[AcpRuntimeEvent]:
        async def _gen() -> AsyncIterator[AcpRuntimeEvent]:
            yield AcpEventTextDelta(text=input.text, stream="output")
            yield AcpEventDone(stop_reason="stop")

        return _gen()

    async def cancel(
        self, *, handle: AcpRuntimeHandle, reason: str | None = None
    ) -> None:
        return None

    async def close(
        self,
        *,
        handle: AcpRuntimeHandle,
        reason: str,
        discard_persistent_state: bool = False,
    ) -> None:
        return None


def test_minimal_fake_satisfies_acp_runtime_protocol() -> None:
    fake = _MinimalFakeRuntime()
    assert isinstance(fake, AcpRuntime)
    # Fake omits the optional surface intentionally — it should
    # NOT satisfy AcpRuntimeOptional. This pins the split.
    assert not isinstance(fake, AcpRuntimeOptional)


def test_official_session_update_tags_match_openclaw_set() -> None:
    # Mirrors openclaw runtime/types.ts:5-16. If openclaw adds a tag
    # we should know — this set is the authoritative wire constant.
    assert "agent_message_chunk" in OFFICIAL_SESSION_UPDATE_TAGS
    assert "tool_call_update" in OFFICIAL_SESSION_UPDATE_TAGS
    assert "plan" in OFFICIAL_SESSION_UPDATE_TAGS
    assert len(OFFICIAL_SESSION_UPDATE_TAGS) == 10


async def test_fake_run_turn_emits_text_delta_then_done() -> None:
    fake = _MinimalFakeRuntime()
    handle = await fake.ensure_session(
        AcpRuntimeEnsureInput(session_key="s1", agent="a", mode="oneshot")
    )
    events: list[AcpRuntimeEvent] = []
    async for ev in fake.run_turn(
        AcpRuntimeTurnInput(
            handle=handle, text="hi", mode="prompt", request_id="r1"
        )
    ):
        events.append(ev)
    assert len(events) == 2
    first = events[0]
    last = events[1]
    assert isinstance(first, AcpEventTextDelta)
    assert first.text == "hi"
    assert isinstance(last, AcpEventDone)
    assert last.stop_reason == "stop"


def test_event_dataclasses_carry_their_type_discriminator() -> None:
    assert AcpEventTextDelta(text="x").type == "text_delta"
    assert AcpEventDone().type == "done"
    assert AcpEventError(message="boom").type == "error"
