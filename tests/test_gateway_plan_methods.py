"""Tests for `plan.get` / `plan.list` RPCs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.gateway.router import Router
from oxenclaw.gateway.plan_methods import register_plan_methods
from oxenclaw.tools_pkg.update_plan_tool import update_plan_tool


def _paths(tmp: Path) -> OxenclawPaths:
    p = OxenclawPaths(home=tmp)
    p.ensure_home()
    return p


def _steps(statuses: list[str]) -> list[dict]:
    return [
        {"id": str(i + 1), "title": f"Step {i + 1}", "status": s, "notes": ""}
        for i, s in enumerate(statuses)
    ]


@pytest.fixture()
def router_and_paths(tmp_path: Path):
    paths = _paths(tmp_path)
    r = Router()
    register_plan_methods(r, paths=paths)
    return r, paths


async def test_plan_get_rpc_returns_persisted_steps(router_and_paths) -> None:
    """Write a plan via the tool, then call the RPC and verify the steps are returned."""
    r, paths = router_and_paths
    tool = update_plan_tool(paths=paths)

    await tool.execute(
        {
            "session_key": "sess1",
            "agent_id": "assistant",
            "title": "Test Plan",
            "steps": _steps(["completed", "in_progress", "pending"]),
        }
    )

    resp = await r.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "plan.get",
            "params": {"agent_id": "assistant", "session_key": "sess1"},
        }
    )
    assert resp.error is None
    assert len(resp.result["steps"]) == 3
    assert resp.result["steps"][0]["status"] == "completed"
    assert resp.result["title"] == "Test Plan"
    assert resp.result["session_key"] == "sess1"


async def test_plan_list_rpc_aggregates_across_sessions(router_and_paths) -> None:
    """Two sessions for one agent; plan.list must return both rows."""
    r, paths = router_and_paths
    tool = update_plan_tool(paths=paths)

    await tool.execute(
        {
            "session_key": "k1",
            "agent_id": "assistant",
            "steps": _steps(["completed", "completed"]),
        }
    )
    await tool.execute(
        {
            "session_key": "k2",
            "agent_id": "assistant",
            "steps": _steps(["pending", "in_progress"]),
        }
    )

    resp = await r.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "plan.list",
            "params": {"agent_id": "assistant"},
        }
    )
    assert resp.error is None
    plans = resp.result["plans"]
    assert len(plans) == 2
    by_key = {row["session_key"]: row for row in plans}
    assert by_key["k1"]["completed"] == 2
    assert by_key["k2"]["pending"] == 1
    assert by_key["k2"]["in_progress"] == 1


async def test_plan_get_returns_empty_when_missing(router_and_paths) -> None:
    """Missing plan file must return empty steps array without error."""
    r, paths = router_and_paths

    resp = await r.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "plan.get",
            "params": {"agent_id": "nobody", "session_key": "no-such-session"},
        }
    )
    assert resp.error is None
    assert resp.result == {"steps": []}


async def test_plan_list_returns_empty_when_no_plans(router_and_paths) -> None:
    """Listing plans for an agent with no session dir returns an empty list."""
    r, paths = router_and_paths

    resp = await r.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "plan.list",
            "params": {"agent_id": "ghost"},
        }
    )
    assert resp.error is None
    assert resp.result["plans"] == []


async def test_plan_list_all_agents(router_and_paths) -> None:
    """plan.list with no agent_id returns plans across all agents."""
    r, paths = router_and_paths
    tool = update_plan_tool(paths=paths)

    await tool.execute(
        {"session_key": "s1", "agent_id": "alpha", "steps": _steps(["completed"])}
    )
    await tool.execute(
        {"session_key": "s2", "agent_id": "beta", "steps": _steps(["pending"])}
    )

    resp = await r.dispatch(
        {"jsonrpc": "2.0", "id": 5, "method": "plan.list", "params": {}}
    )
    assert resp.error is None
    agent_ids = {row["agent_id"] for row in resp.result["plans"]}
    assert "alpha" in agent_ids
    assert "beta" in agent_ids
