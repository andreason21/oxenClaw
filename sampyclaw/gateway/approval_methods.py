"""exec-approvals.* JSON-RPC methods bound to an ApprovalManager."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from sampyclaw.approvals import ApprovalManager
from sampyclaw.gateway.router import Router


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


def register_approval_methods(router: Router, manager: ApprovalManager) -> None:
    @router.method("exec-approvals.list")
    async def _list(_: dict) -> list[dict]:  # type: ignore[type-arg]
        return [r.model_dump() for r in manager.list()]

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
        return {
            "cancelled": manager.cancel(
                p.id, reason=p.reason, approver_token=p.approver_token
            )
        }
