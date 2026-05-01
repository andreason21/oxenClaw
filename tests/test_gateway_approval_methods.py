"""Tests for exec-approvals.* gateway RPCs."""

from __future__ import annotations

import asyncio

from oxenclaw.approvals import ApprovalManager
from oxenclaw.gateway.approval_methods import register_approval_methods
from oxenclaw.gateway.router import Router


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


async def test_tools_lists_gated_tools_per_agent() -> None:
    """exec-approvals.tools enumerates every gated tool and the agents
    that expose it, deduped across agents."""
    from oxenclaw.agents.registry import AgentRegistry
    from oxenclaw.agents.tools import Tool, ToolRegistry
    from oxenclaw.approvals.tool_wrap import gated_tool

    class _Stub:
        def __init__(self, name: str) -> None:
            self.name = name
            self.description = f"{name} desc"
            self.input_schema = {"type": "object", "properties": {}}

        async def execute(self, args: dict) -> str:  # type: ignore[type-arg]
            return "ok"

    class _StubAgent:
        def __init__(self, agent_id: str, tools: ToolRegistry) -> None:
            self.id = agent_id
            self._tools = tools

        async def handle(self, *_a, **_k):  # pragma: no cover — unused
            yield None

    manager = ApprovalManager()
    raw_a: Tool = _Stub("write_file")  # type: ignore[assignment]
    raw_b: Tool = _Stub("shell_run")  # type: ignore[assignment]

    reg_one = ToolRegistry()
    reg_one.register(gated_tool(raw_a, manager=manager))
    reg_one.register(gated_tool(raw_b, manager=manager))

    reg_two = ToolRegistry()
    reg_two.register(gated_tool(_Stub("write_file"), manager=manager))  # type: ignore[arg-type]

    agents = AgentRegistry()
    agents.register(_StubAgent("alpha", reg_one))  # type: ignore[arg-type]
    agents.register(_StubAgent("beta", reg_two))  # type: ignore[arg-type]

    router = Router()
    from oxenclaw.gateway.approval_methods import register_approval_methods

    register_approval_methods(router, manager, agents=agents)

    resp = await router.dispatch({"jsonrpc": "2.0", "id": 1, "method": "exec-approvals.tools"})
    by_name = {t["name"]: t for t in resp.result}
    assert set(by_name) == {"write_file", "shell_run"}
    assert by_name["write_file"]["agents"] == ["alpha", "beta"]
    assert by_name["shell_run"]["agents"] == ["alpha"]
    assert by_name["write_file"]["description"] == "write_file desc"


async def test_tools_returns_empty_when_no_agents_passed() -> None:
    router, _ = _setup()
    resp = await router.dispatch({"jsonrpc": "2.0", "id": 1, "method": "exec-approvals.tools"})
    assert resp.result == []
