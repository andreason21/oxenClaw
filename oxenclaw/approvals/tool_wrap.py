"""Wrap a Tool so execute() blocks on approval.

Usage:
    safe = gated_tool(raw_shell_tool, manager=approvals, format_prompt=my_fmt)
    registry.register(safe)

When the model calls the wrapped tool, it receives a "denied" error string
back instead of the tool output, unless the human operator resolves the
approval affirmatively first.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from oxenclaw.agents.tools import Tool
from oxenclaw.approvals.manager import ApprovalManager
from oxenclaw.approvals.models import ApprovalStatus


def default_format_prompt(tool_name: str, args: dict[str, Any]) -> str:
    return f"Approve tool call {tool_name!r} with args {args!r}?"


class _GatedTool:
    def __init__(
        self,
        wrapped: Tool,
        *,
        manager: ApprovalManager,
        format_prompt: Callable[[str, dict[str, Any]], str] = default_format_prompt,
        timeout: float | None = None,
    ) -> None:
        self._wrapped = wrapped
        self._manager = manager
        self._format = format_prompt
        self._timeout = timeout

    @property
    def name(self) -> str:
        return self._wrapped.name

    @property
    def description(self) -> str:
        return f"{self._wrapped.description} (requires human approval before execution)"

    @property
    def input_schema(self) -> dict[str, Any]:
        return self._wrapped.input_schema

    async def execute(self, args: dict[str, Any]) -> str:
        result = await self._manager.request(
            self._format(self._wrapped.name, args),
            context={"tool": self._wrapped.name, "args": args},
            timeout=self._timeout,
        )
        if result.status is ApprovalStatus.APPROVED:
            return await self._wrapped.execute(args)
        # Distinguish outcomes so the model can react appropriately:
        #   DENIED    → user said no; don't retry the same call
        #   TIMED_OUT → no human was watching; try again later or ask
        #   CANCELLED → operator cancelled (e.g. shutdown); benign
        verb = {
            ApprovalStatus.DENIED: "denied by approver",
            ApprovalStatus.TIMED_OUT: "no approver responded in time",
            ApprovalStatus.CANCELLED: "approval cancelled",
        }.get(result.status, f"not approved ({result.status.value})")
        return f"tool call {verb}" + (f": {result.reason}" if result.reason else "")


def gated_tool(
    tool: Tool,
    *,
    manager: ApprovalManager,
    format_prompt: Callable[[str, dict[str, Any]], str] | None = None,
    timeout: float | None = None,
) -> Tool:
    return _GatedTool(
        tool,
        manager=manager,
        format_prompt=format_prompt or default_format_prompt,
        timeout=timeout,
    )
