"""exec-approvals.* JSON-RPC methods bound to an ApprovalManager."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from oxenclaw.agents.registry import AgentRegistry
from oxenclaw.agents.tools import ToolRegistry
from oxenclaw.approvals import ApprovalManager
from oxenclaw.gateway.router import Router


class _ResolveParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    approved: bool
    reason: str | None = None
    approver_token: str | None = None


class _CancelParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    reason: str | None = None
    approver_token: str | None = None


def _is_gated(tool: Any) -> bool:
    return bool(getattr(tool, "is_gated", False))


def _collect_gated_tools(agents: AgentRegistry | None) -> list[dict[str, Any]]:
    """Walk every agent's ToolRegistry and report tools wrapped via gated_tool().

    Tools shared across agents are deduped by name; the agent ids that
    expose each one are listed so operators can see the surface area at
    a glance.
    """
    if agents is None:
        return []
    seen: dict[str, dict[str, Any]] = {}
    for agent_id, agent in agents.items():
        registry: ToolRegistry | None = getattr(agent, "_tools", None)
        if registry is None:
            continue
        for tool in registry.tools():
            if not _is_gated(tool):
                continue
            entry = seen.get(tool.name)
            if entry is None:
                wrapped = getattr(tool, "wrapped", None)
                seen[tool.name] = {
                    "name": tool.name,
                    "description": getattr(wrapped, "description", tool.description),
                    "agents": [agent_id],
                }
            elif agent_id not in entry["agents"]:
                entry["agents"].append(agent_id)
    return [seen[n] for n in sorted(seen)]


def register_approval_methods(
    router: Router,
    manager: ApprovalManager,
    *,
    agents: AgentRegistry | None = None,
) -> None:
    @router.method("exec-approvals.list")
    async def _list(_: dict) -> list[dict]:  # type: ignore[type-arg]
        return [r.model_dump() for r in manager.list()]

    @router.method("exec-approvals.tools")
    async def _tools(_: dict) -> list[dict]:  # type: ignore[type-arg]
        return _collect_gated_tools(agents)

    @router.method("exec-approvals.resolve", _ResolveParams)
    async def _resolve(p: _ResolveParams) -> dict:  # type: ignore[type-arg]
        result = manager.resolve(
            p.id,
            approved=p.approved,
            reason=p.reason,
            approver_token=p.approver_token,
        )
        return {
            "resolved": result is not None,
            "status": result.status.value if result else None,
        }

    @router.method("exec-approvals.cancel", _CancelParams)
    async def _cancel(p: _CancelParams) -> dict:  # type: ignore[type-arg]
        return {"cancelled": manager.cancel(p.id, reason=p.reason, approver_token=p.approver_token)}
