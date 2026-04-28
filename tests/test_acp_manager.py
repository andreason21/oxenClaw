"""End-to-end tests for AcpSessionManager + registry + fake runtime.

Pins the integration shape: a backend registered in the registry,
opened via the manager, prompted via `run_turn`, cancelled, and
closed — without any subprocess or real wire I/O.
"""

from __future__ import annotations

import pytest

from oxenclaw.acp import manager as manager_mod
from oxenclaw.acp import runtime_registry as registry_mod
from oxenclaw.acp.fake_runtime import InMemoryFakeRuntime
from oxenclaw.acp.manager import (
    AcpCloseSessionInput,
    AcpInitializeSessionInput,
    AcpManagerError,
    AcpRunTurnInput,
    get_acp_session_manager,
)
from oxenclaw.acp.runtime_registry import (
    AcpRegistryError,
    AcpRuntimeBackend,
    get_acp_runtime_backend,
    list_acp_runtime_backends,
    register_acp_runtime_backend,
    require_acp_runtime_backend,
    unregister_acp_runtime_backend,
)
from oxenclaw.agents.acp_runtime import (
    AcpEventDone,
    AcpEventStatus,
    AcpEventTextDelta,
    AcpRuntimeEvent,
)


@pytest.fixture(autouse=True)
def _isolate_globals():
    """Each test starts with a fresh registry + manager singleton."""
    registry_mod.reset_for_tests()
    manager_mod.reset_for_tests()
    yield
    registry_mod.reset_for_tests()
    manager_mod.reset_for_tests()


# --- registry --------------------------------------------------------------


def test_register_and_resolve_by_id() -> None:
    rt = InMemoryFakeRuntime()
    register_acp_runtime_backend(AcpRuntimeBackend(id="fake", runtime=rt))
    found = get_acp_runtime_backend("fake")
    assert found is not None
    assert found.runtime is rt


def test_register_normalises_id_to_lowercase() -> None:
    register_acp_runtime_backend(AcpRuntimeBackend(id="MIXED", runtime=InMemoryFakeRuntime()))
    assert get_acp_runtime_backend("mixed") is not None
    assert get_acp_runtime_backend("MIXED") is not None
    assert "mixed" in list_acp_runtime_backends()


def test_register_rejects_empty_id() -> None:
    with pytest.raises(AcpRegistryError, match="id is required"):
        register_acp_runtime_backend(AcpRuntimeBackend(id="", runtime=InMemoryFakeRuntime()))


def test_resolve_without_id_returns_first_healthy_backend() -> None:
    a = InMemoryFakeRuntime()
    b = InMemoryFakeRuntime()
    register_acp_runtime_backend(AcpRuntimeBackend(id="a", runtime=a, healthy=lambda: False))
    register_acp_runtime_backend(AcpRuntimeBackend(id="b", runtime=b))
    found = get_acp_runtime_backend(None)
    assert found is not None
    assert found.runtime is b


def test_require_raises_when_unknown_id() -> None:
    with pytest.raises(AcpRegistryError, match="not registered"):
        require_acp_runtime_backend("nope")


def test_unregister_removes_backend() -> None:
    register_acp_runtime_backend(AcpRuntimeBackend(id="x", runtime=InMemoryFakeRuntime()))
    unregister_acp_runtime_backend("x")
    assert get_acp_runtime_backend("x") is None


# --- manager singleton ----------------------------------------------------


def test_singleton_is_stable_within_a_session() -> None:
    a = get_acp_session_manager()
    b = get_acp_session_manager()
    assert a is b


def test_reset_for_tests_drops_singleton() -> None:
    a = get_acp_session_manager()
    manager_mod.reset_for_tests()
    b = get_acp_session_manager()
    assert a is not b


# --- e2e ------------------------------------------------------------------


async def test_initialize_run_turn_close_happy_path() -> None:
    rt = InMemoryFakeRuntime()
    register_acp_runtime_backend(AcpRuntimeBackend(id="fake", runtime=rt))
    mgr = get_acp_session_manager()

    handle = await mgr.initialize_session(
        AcpInitializeSessionInput(
            session_key="agent:42:fake:1",
            agent="echo-agent",
            mode="oneshot",
            backend_id="fake",
        )
    )
    assert handle.backend == "fake"
    assert handle.session_key == "agent:42:fake:1"
    assert mgr.has_session("agent:42:fake:1")

    events: list[AcpRuntimeEvent] = []
    async for ev in mgr.run_turn(
        AcpRunTurnInput(
            session_key="agent:42:fake:1",
            text="hello",
            request_id="req-1",
        )
    ):
        events.append(ev)
    assert len(events) == 2
    first, last = events
    assert isinstance(first, AcpEventTextDelta)
    assert first.text == "hello"
    assert isinstance(last, AcpEventDone)
    assert last.stop_reason == "stop"

    await mgr.close_session(
        AcpCloseSessionInput(session_key="agent:42:fake:1", reason="client_close")
    )
    assert not mgr.has_session("agent:42:fake:1")


async def test_initialize_idempotent_returns_same_handle() -> None:
    rt = InMemoryFakeRuntime()
    register_acp_runtime_backend(AcpRuntimeBackend(id="fake", runtime=rt))
    mgr = get_acp_session_manager()

    h1 = await mgr.initialize_session(
        AcpInitializeSessionInput(session_key="s", agent="a", backend_id="fake")
    )
    h2 = await mgr.initialize_session(
        AcpInitializeSessionInput(session_key="s", agent="a", backend_id="fake")
    )
    assert h1 is h2 or h1 == h2


async def test_initialize_rejects_backend_swap_on_existing_session() -> None:
    register_acp_runtime_backend(AcpRuntimeBackend(id="fake", runtime=InMemoryFakeRuntime()))
    register_acp_runtime_backend(AcpRuntimeBackend(id="fake2", runtime=InMemoryFakeRuntime()))
    mgr = get_acp_session_manager()
    await mgr.initialize_session(
        AcpInitializeSessionInput(session_key="s", agent="a", backend_id="fake")
    )
    with pytest.raises(AcpManagerError, match="refusing rebind"):
        await mgr.initialize_session(
            AcpInitializeSessionInput(session_key="s", agent="a", backend_id="fake2")
        )


async def test_run_turn_on_unknown_session_raises() -> None:
    register_acp_runtime_backend(AcpRuntimeBackend(id="fake", runtime=InMemoryFakeRuntime()))
    mgr = get_acp_session_manager()
    with pytest.raises(AcpManagerError, match="not initialised"):
        async for _ in mgr.run_turn(
            AcpRunTurnInput(session_key="missing", text="x", request_id="r")
        ):
            pass


async def test_cancel_makes_next_turn_emit_cancel_done() -> None:
    rt = InMemoryFakeRuntime()
    register_acp_runtime_backend(AcpRuntimeBackend(id="fake", runtime=rt))
    mgr = get_acp_session_manager()
    handle = await mgr.initialize_session(
        AcpInitializeSessionInput(session_key="s", agent="a", backend_id="fake")
    )
    # Pre-cancel so the next turn observes the flag at its first yield.
    await rt.cancel(handle=handle)
    events: list[AcpRuntimeEvent] = []
    async for ev in mgr.run_turn(AcpRunTurnInput(session_key="s", text="t", request_id="r")):
        events.append(ev)
    assert len(events) == 1
    assert isinstance(events[0], AcpEventDone)
    assert events[0].stop_reason == "cancel"


async def test_close_session_is_idempotent_after_already_closed() -> None:
    register_acp_runtime_backend(AcpRuntimeBackend(id="fake", runtime=InMemoryFakeRuntime()))
    mgr = get_acp_session_manager()
    await mgr.initialize_session(
        AcpInitializeSessionInput(session_key="s", agent="a", backend_id="fake")
    )
    await mgr.close_session(AcpCloseSessionInput(session_key="s"))
    # Second close on an unknown session is a silent no-op.
    await mgr.close_session(AcpCloseSessionInput(session_key="s"))
    assert not mgr.has_session("s")


async def test_scripted_events_drive_turn_output() -> None:
    rt = InMemoryFakeRuntime()
    register_acp_runtime_backend(AcpRuntimeBackend(id="fake", runtime=rt))
    mgr = get_acp_session_manager()
    await mgr.initialize_session(
        AcpInitializeSessionInput(session_key="s", agent="a", backend_id="fake")
    )
    rt.script_session(
        "s",
        [
            AcpEventStatus(text="thinking", tag="agent_thought_chunk"),
            AcpEventTextDelta(text="line1\n", stream="output"),
            AcpEventTextDelta(text="line2", stream="output"),
            AcpEventDone(stop_reason="stop"),
        ],
    )
    events: list[AcpRuntimeEvent] = []
    async for ev in mgr.run_turn(AcpRunTurnInput(session_key="s", text="ignored", request_id="r")):
        events.append(ev)
    assert len(events) == 4
    assert isinstance(events[0], AcpEventStatus)
    assert events[0].tag == "agent_thought_chunk"
    assert isinstance(events[-1], AcpEventDone)


async def test_observability_snapshot_lists_live_sessions() -> None:
    register_acp_runtime_backend(AcpRuntimeBackend(id="fake", runtime=InMemoryFakeRuntime()))
    mgr = get_acp_session_manager()
    await mgr.initialize_session(
        AcpInitializeSessionInput(session_key="s1", agent="a", backend_id="fake")
    )
    await mgr.initialize_session(
        AcpInitializeSessionInput(session_key="s2", agent="a", backend_id="fake")
    )
    snap = mgr.observability_snapshot()
    assert snap.sessions == {"s1": "fake", "s2": "fake"}
    assert sorted(snap.backends) == ["fake", "fake"]


async def test_initialize_without_backend_id_uses_first_healthy() -> None:
    register_acp_runtime_backend(
        AcpRuntimeBackend(
            id="unhealthy",
            runtime=InMemoryFakeRuntime(),
            healthy=lambda: False,
        )
    )
    register_acp_runtime_backend(AcpRuntimeBackend(id="healthy", runtime=InMemoryFakeRuntime()))
    mgr = get_acp_session_manager()
    await mgr.initialize_session(AcpInitializeSessionInput(session_key="s", agent="a"))
    # `handle.backend` is the runtime's self-reported id (both fakes
    # report "fake"); the registry binding is what the manager uses
    # to route, so assert on the observability snapshot instead.
    snap = mgr.observability_snapshot()
    assert snap.sessions["s"] == "healthy"
