"""Human-in-the-loop approval for sensitive tool calls.

Agents wrap risky tools with `gated_tool(...)`; when the agent tries to
invoke the wrapped tool, the tool awaits `ApprovalManager.request(...)`.
An external client (CLI, channel callback, UI) resolves the request via
`exec-approvals.resolve` gateway RPC, which wakes the tool up and lets it
proceed (or return an error string).
"""

from oxenclaw.approvals.manager import ApprovalManager
from oxenclaw.approvals.models import (
    ApprovalRequest,
    ApprovalResult,
    ApprovalStatus,
)
from oxenclaw.approvals.tool_wrap import gated_tool

__all__ = [
    "ApprovalManager",
    "ApprovalRequest",
    "ApprovalResult",
    "ApprovalStatus",
    "gated_tool",
]
