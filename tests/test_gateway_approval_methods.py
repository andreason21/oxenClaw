"""Tests for exec-approvals.* gateway RPCs."""

from __future__ import annotations

import asyncio

from sampyclaw.approvals import ApprovalManager
from sampyclaw.gateway.approval_methods import register_approval_methods
from sampyclaw.gateway.router import Router


def _setup() -> tuple[Router, ApprovalManager]:
    manager = ApprovalManager()
    router = Router()
    register_approval_methods(router, manager)
    return router, manager


async def test_list_reports_pending() -> None:
    router, manager = _setup()
    task = asyncio.create_task(manager.request("ok?"))
    await asyncio.sleep(0)
    resp = await router.dispatch({"jsonrpc": "2.0", "id": 1, "method": "exec-approvals.list"})
    assert len(resp.result) == 1
    pid = resp.result[0]["id"]
    manager.resolve(pid, approved=True)
    await task


async def test_resolve_approves() -> None:
    router, manager = _setup()
    task = asyncio.create_task(manager.request("ok?"))
    await asyncio.sleep(0)
    pid = manager.list()[0].id
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "exec-approvals.resolve",
            "params": {"id": pid, "approved": True},
        }
    )
    assert resp.result == {"resolved": True, "status": "approved"}
    result = await task
    assert result.approved is True


async def test_resolve_unknown_is_not_resolved() -> None:
    router, _ = _setup()
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "exec-approvals.resolve",
            "params": {"id": "nope", "approved": True},
        }
    )
    assert resp.result == {"resolved": False, "status": None}


async def test_cancel_completes_future() -> None:
    router, manager = _setup()
    task = asyncio.create_task(manager.request("ok?"))
    await asyncio.sleep(0)
    pid = manager.list()[0].id
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "exec-approvals.cancel",
            "params": {"id": pid, "reason": "shutdown"},
        }
    )
    assert resp.result == {"cancelled": True}
    result = await task
    assert result.status.value == "cancelled"


async def test_resolve_validates_params() -> None:
    router, _ = _setup()
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "exec-approvals.resolve",
            "params": {"id": "x"},  # missing approved
        }
    )
    assert resp.error is not None
