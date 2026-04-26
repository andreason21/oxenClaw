"""Tests for ApprovalManager: request/resolve/cancel/timeout + event emission."""

from __future__ import annotations

import asyncio
import json

import pytest

from oxenclaw.approvals.manager import ApprovalAuthError, ApprovalManager
from oxenclaw.approvals.models import ApprovalStatus


async def _request(manager: ApprovalManager, prompt: str = "ok?"):  # type: ignore[no-untyped-def]
    return asyncio.create_task(manager.request(prompt))


async def test_resolve_approved() -> None:
    m = ApprovalManager()
    task = await _request(m)
    # Give request time to register.
    await asyncio.sleep(0)
    pending = m.list()
    assert len(pending) == 1
    assert m.resolve(pending[0].id, approved=True).status is ApprovalStatus.APPROVED
    result = await task
    assert result.approved is True


async def test_resolve_denied() -> None:
    m = ApprovalManager()
    task = await _request(m)
    await asyncio.sleep(0)
    pid = m.list()[0].id
    m.resolve(pid, approved=False, reason="no")
    result = await task
    assert result.status is ApprovalStatus.DENIED
    assert result.reason == "no"


async def test_cancel_completes_future() -> None:
    m = ApprovalManager()
    task = await _request(m)
    await asyncio.sleep(0)
    pid = m.list()[0].id
    assert m.cancel(pid) is True
    result = await task
    assert result.status is ApprovalStatus.CANCELLED


async def test_timeout_completes_with_timed_out() -> None:
    m = ApprovalManager()
    result = await m.request("quick?", timeout=0.01)
    assert result.status is ApprovalStatus.TIMED_OUT


async def test_resolve_twice_returns_none_on_second() -> None:
    m = ApprovalManager()
    task = await _request(m)
    await asyncio.sleep(0)
    pid = m.list()[0].id
    assert m.resolve(pid, approved=True) is not None
    await task  # wait for cleanup
    assert m.resolve(pid, approved=True) is None


async def test_resolve_unknown_returns_none() -> None:
    m = ApprovalManager()
    assert m.resolve("nope", approved=True) is None
    assert m.cancel("nope") is False


async def test_request_removes_from_pending_after_resolve() -> None:
    m = ApprovalManager()
    task = await _request(m)
    await asyncio.sleep(0)
    pid = m.list()[0].id
    m.resolve(pid, approved=True)
    await task
    assert m.list() == []


async def test_cancel_all_resolves_every_pending() -> None:
    m = ApprovalManager()
    tasks = [asyncio.create_task(m.request(f"q{i}")) for i in range(3)]
    await asyncio.sleep(0)
    assert m.cancel_all(reason="shutdown") == 3
    results = await asyncio.gather(*tasks)
    assert all(r.status is ApprovalStatus.CANCELLED for r in results)


async def test_on_event_called_on_request_and_close() -> None:
    events: list[tuple[str, dict]] = []

    async def _on_event(kind: str, payload: dict) -> None:  # type: ignore[type-arg]
        events.append((kind, payload))

    m = ApprovalManager(on_event=_on_event)
    task = await _request(m)
    await asyncio.sleep(0)
    pid = m.list()[0].id
    m.resolve(pid, approved=True)
    await task

    kinds = [e[0] for e in events]
    assert kinds == ["approval.requested", "approval.closed"]
    assert events[0][1]["prompt"] == "ok?"


# ─── identity binding ───


async def test_resolve_requires_token_when_configured() -> None:
    m = ApprovalManager(approver_token="t0p-secret")
    task = await _request(m)
    await asyncio.sleep(0)
    pid = m.list()[0].id
    with pytest.raises(ApprovalAuthError):
        m.resolve(pid, approved=True)
    with pytest.raises(ApprovalAuthError):
        m.resolve(pid, approved=True, approver_token="wrong")
    res = m.resolve(pid, approved=True, approver_token="t0p-secret")
    assert res is not None and res.status is ApprovalStatus.APPROVED
    await task


async def test_cancel_requires_token_when_configured() -> None:
    m = ApprovalManager(approver_token="t0p-secret")
    task = await _request(m)
    await asyncio.sleep(0)
    pid = m.list()[0].id
    with pytest.raises(ApprovalAuthError):
        m.cancel(pid, reason="nope")
    assert m.cancel(pid, approver_token="t0p-secret") is True
    await task


async def test_cancel_all_bypasses_token() -> None:
    """cancel_all is the shutdown path; an internal caller should not need
    to know the operator token to clean up its own pending requests."""
    m = ApprovalManager(approver_token="t0p-secret")
    tasks = [await _request(m), await _request(m)]
    await asyncio.sleep(0)
    assert m.cancel_all() == 2
    results = await asyncio.gather(*tasks)
    assert all(r.status is ApprovalStatus.CANCELLED for r in results)


# ─── persistence ───


async def test_state_path_persists_pending(tmp_path) -> None:  # type: ignore[no-untyped-def]
    state = tmp_path / "approvals.json"
    m = ApprovalManager(state_path=state)
    task = await _request(m)
    await asyncio.sleep(0)
    assert state.exists()
    snap = json.loads(state.read_text())
    assert len(snap["pending"]) == 1
    assert snap["pending"][0]["prompt"] == "ok?"
    pid = m.list()[0].id
    m.resolve(pid, approved=True)
    await task
    # After resolution, the snapshot reflects an empty pending list.
    snap2 = json.loads(state.read_text())
    assert snap2["pending"] == []


async def test_state_path_recovers_into_audit_log(tmp_path, caplog) -> None:  # type: ignore[no-untyped-def]
    import logging as _logging

    caplog.set_level(_logging.INFO, logger="oxenclaw.approvals.manager")
    state = tmp_path / "approvals.json"
    state.write_text(
        json.dumps(
            {
                "pending": [
                    {
                        "id": "abc",
                        "prompt": "carryover?",
                        "context": {},
                        "requested_at": 0.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    ApprovalManager(state_path=state)
    assert any("carried over" in r.message for r in caplog.records)
    # Stale snapshot is cleared so we don't recreate the warning every restart.
    assert not state.exists()
