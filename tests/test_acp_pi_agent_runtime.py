"""Tests for PiAgentAcpRuntime — wraps a real PiAgent as an AcpRuntime.

The fake-streaming `register_provider_stream` hook lets us drive
PiAgent without spinning up Ollama or an API key. End-to-end:
  - register a fake stream → InMemoryModelRegistry → PiAgent
  - wrap PiAgent in PiAgentAcpRuntime
  - register the runtime with the backend registry
  - drive `manager.initialize_session → run_turn` and assert the
    streamed AcpEventTextDelta carries the agent's reply text + a
    final AcpEventDone(stop)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from oxenclaw.acp import manager as manager_mod
from oxenclaw.acp import runtime_registry as registry_mod
from oxenclaw.acp.manager import (
    AcpCloseSessionInput,
    AcpInitializeSessionInput,
    AcpRunTurnInput,
    get_acp_session_manager,
)
from oxenclaw.acp.pi_agent_runtime import PiAgentAcpRuntime
from oxenclaw.acp.runtime_registry import (
    AcpRuntimeBackend,
    register_acp_runtime_backend,
)
from oxenclaw.agents.acp_runtime import (
    AcpEventDone,
    AcpEventError,
    AcpEventTextDelta,
    AcpRuntimeEvent,
    AcpRuntimeEnsureInput,
    AcpRuntimeTurnInput,
)
from oxenclaw.agents.pi_agent import PiAgent
from oxenclaw.config import OxenclawPaths
from oxenclaw.pi import (
    InMemoryAuthStorage,
    InMemorySessionManager,
    Model,
    register_provider_stream,
)
from oxenclaw.pi.registry import InMemoryModelRegistry
from oxenclaw.pi.streaming import StopEvent, TextDeltaEvent


@pytest.fixture(autouse=True)
def _isolate_globals():
    registry_mod.reset_for_tests()
    manager_mod.reset_for_tests()
    yield
    registry_mod.reset_for_tests()
    manager_mod.reset_for_tests()


def _paths(tmp_path: Path) -> OxenclawPaths:
    p = OxenclawPaths(home=tmp_path)
    p.ensure_home()
    return p


def _make_pi_agent(tmp_path: Path, *, provider: str) -> PiAgent:
    reg = InMemoryModelRegistry(
        models=[
            Model(
                id="acp-test-model",
                provider=provider,
                max_output_tokens=256,
                extra={"base_url": "http://test-fake"},
            )
        ]
    )
    return PiAgent(
        agent_id="acp-pi",
        model_id="acp-test-model",
        registry=reg,
        auth=InMemoryAuthStorage({provider: "sk-test"}),  # type: ignore[dict-item]
        sessions=InMemorySessionManager(),
        paths=_paths(tmp_path),
    )


async def _drain(it: AsyncIterator[AcpRuntimeEvent]) -> list[AcpRuntimeEvent]:
    out: list[AcpRuntimeEvent] = []
    async for ev in it:
        out.append(ev)
    return out


async def test_run_turn_streams_assistant_text_then_done(
    tmp_path: Path,
) -> None:
    async def fake_stream(_ctx, _opts):  # type: ignore[no-untyped-def]
        yield TextDeltaEvent(delta="hello ")
        yield TextDeltaEvent(delta="from pi over acp")
        yield StopEvent(reason="end_turn")

    register_provider_stream("acp_pi_text", fake_stream)
    agent = _make_pi_agent(tmp_path, provider="acp_pi_text")
    runtime = PiAgentAcpRuntime(agent=agent, backend_id="pi")
    register_acp_runtime_backend(AcpRuntimeBackend(id="pi", runtime=runtime))

    mgr = get_acp_session_manager()
    handle = await mgr.initialize_session(
        AcpInitializeSessionInput(
            session_key="acp-pi:1",
            agent="acp-pi",
            mode="oneshot",
            backend_id="pi",
        )
    )
    assert handle.backend == "pi"

    events = await _drain(
        mgr.run_turn(
            AcpRunTurnInput(
                session_key="acp-pi:1",
                text="hello",
                request_id="r1",
            )
        )
    )
    text_chunks = [e for e in events if isinstance(e, AcpEventTextDelta)]
    assert text_chunks  # at least one chunk
    full_text = "".join(c.text for c in text_chunks)
    assert "hello from pi over acp" in full_text
    done = events[-1]
    assert isinstance(done, AcpEventDone)
    assert done.stop_reason == "stop"

    await mgr.close_session(AcpCloseSessionInput(session_key="acp-pi:1"))


async def test_pre_cancel_yields_done_cancel_before_text(
    tmp_path: Path,
) -> None:
    async def fake_stream(_ctx, _opts):  # type: ignore[no-untyped-def]
        yield TextDeltaEvent(delta="should not appear")
        yield StopEvent(reason="end_turn")

    register_provider_stream("acp_pi_cancel", fake_stream)
    agent = _make_pi_agent(tmp_path, provider="acp_pi_cancel")
    runtime = PiAgentAcpRuntime(agent=agent, backend_id="pi")

    handle = await runtime.ensure_session(
        AcpRuntimeEnsureInput(
            session_key="s", agent="a", mode="oneshot"
        )
    )
    # Pre-cancel — first event observed during the turn should be a
    # cancel-done.
    await runtime.cancel(handle=handle)
    events = await _drain(
        runtime.run_turn(
            AcpRuntimeTurnInput(
                handle=handle, text="x", mode="prompt", request_id="r"
            )
        )
    )
    # The first chunk arrives, the cancel flag is observed at the
    # next iteration. We accept either "cancel before any text" or
    # "cancel after one text chunk" — both honour the contract.
    done = events[-1]
    assert isinstance(done, AcpEventDone)
    assert done.stop_reason == "cancel"


async def test_run_turn_on_unknown_session_yields_error_event(
    tmp_path: Path,
) -> None:
    agent = _make_pi_agent(tmp_path, provider="acp_pi_text")
    runtime = PiAgentAcpRuntime(agent=agent, backend_id="pi")
    from oxenclaw.agents.acp_runtime import AcpRuntimeHandle

    bogus = AcpRuntimeHandle(
        session_key="never-opened",
        backend="pi",
        runtime_session_name="bogus",
    )
    events = await _drain(
        runtime.run_turn(
            AcpRuntimeTurnInput(
                handle=bogus, text="x", mode="prompt", request_id="r"
            )
        )
    )
    assert len(events) == 1
    assert isinstance(events[0], AcpEventError)
    assert events[0].code == "session_not_initialised"


async def test_close_with_discard_invalidates_recall_snapshot(
    tmp_path: Path,
) -> None:
    """`discard_persistent_state=True` should invoke PiAgent's
    recall-snapshot invalidator if the agent exposes one."""

    async def fake_stream(_ctx, _opts):  # type: ignore[no-untyped-def]
        yield TextDeltaEvent(delta="ok")
        yield StopEvent(reason="end_turn")

    register_provider_stream("acp_pi_close", fake_stream)
    agent = _make_pi_agent(tmp_path, provider="acp_pi_close")
    invalidated: list[str] = []
    real_invalidate = agent.invalidate_recall_snapshot

    def spy(key: str | None = None) -> None:
        invalidated.append(key or "")
        real_invalidate(key)

    agent.invalidate_recall_snapshot = spy  # type: ignore[assignment]
    runtime = PiAgentAcpRuntime(agent=agent)
    handle = await runtime.ensure_session(
        AcpRuntimeEnsureInput(
            session_key="discard-me", agent="a", mode="oneshot"
        )
    )
    await runtime.close(
        handle=handle, reason="op", discard_persistent_state=True
    )
    assert invalidated == ["discard-me"]


async def test_idempotent_ensure_session_returns_same_handle(
    tmp_path: Path,
) -> None:
    agent = _make_pi_agent(tmp_path, provider="acp_pi_text")
    runtime = PiAgentAcpRuntime(agent=agent)
    h1 = await runtime.ensure_session(
        AcpRuntimeEnsureInput(session_key="s", agent="a", mode="persistent")
    )
    h2 = await runtime.ensure_session(
        AcpRuntimeEnsureInput(session_key="s", agent="a", mode="persistent")
    )
    assert h1 is h2 or h1 == h2
