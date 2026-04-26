"""Tests for gated_tool(): the approval-gated wrapper around a Tool."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from oxenclaw.agents.tools import FunctionTool
from oxenclaw.approvals import gated_tool
from oxenclaw.approvals.manager import ApprovalManager


class _Args(BaseModel):
    text: str


def _inner() -> FunctionTool:
    def _h(a: _Args) -> str:
        return f"ran:{a.text}"

    return FunctionTool(
        name="shell",
        description="runs a shell command",
        input_model=_Args,
        handler=_h,
    )


async def test_approved_executes_wrapped_tool() -> None:
    manager = ApprovalManager()
    safe = gated_tool(_inner(), manager=manager)

    task = asyncio.create_task(safe.execute({"text": "ls"}))
    await asyncio.sleep(0)
    pid = manager.list()[0].id
    manager.resolve(pid, approved=True)
    result = await task
    assert result == "ran:ls"


async def test_denied_returns_error_string() -> None:
    manager = ApprovalManager()
    safe = gated_tool(_inner(), manager=manager)
    task = asyncio.create_task(safe.execute({"text": "ls"}))
    await asyncio.sleep(0)
    pid = manager.list()[0].id
    manager.resolve(pid, approved=False, reason="no")
    result = await task
    assert "denied" in result
    assert "no" in result


async def test_description_mentions_approval() -> None:
    manager = ApprovalManager()
    safe = gated_tool(_inner(), manager=manager)
    assert "approval" in safe.description.lower()


async def test_name_and_schema_pass_through() -> None:
    manager = ApprovalManager()
    inner = _inner()
    safe = gated_tool(inner, manager=manager)
    assert safe.name == inner.name
    assert safe.input_schema == inner.input_schema


async def test_timeout_surfaces_distinct_from_denial() -> None:
    """Timeout and denial must produce distinct messages so the model can
    react differently (retry vs. don't retry)."""
    manager = ApprovalManager()
    safe = gated_tool(_inner(), manager=manager, timeout=0.01)
    result = await safe.execute({"text": "ls"})
    assert "no approver responded" in result
    assert "denied" not in result


async def test_custom_format_prompt_used() -> None:
    manager = ApprovalManager()

    def _fmt(name: str, args: dict) -> str:  # type: ignore[type-arg]
        return f"Allow {name} with {args['text']}?"

    safe = gated_tool(_inner(), manager=manager, format_prompt=_fmt)
    task = asyncio.create_task(safe.execute({"text": "ls"}))
    await asyncio.sleep(0)
    pending = manager.list()[0]
    assert pending.prompt == "Allow shell with ls?"
    manager.resolve(pending.id, approved=True)
    await task
