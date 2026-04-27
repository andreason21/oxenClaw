"""Small, safe, read-only tools available to every agent by default.

Keep this file tiny — it's the "bootstrap" toolkit. Real tool surface
(shell, filesystem, HTTP) arrives as opt-in tools in later phases.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from oxenclaw.agents.tools import FunctionTool, Tool


class _NoArgs(BaseModel):
    model_config = {"extra": "forbid"}


class _EchoArgs(BaseModel):
    model_config = {"extra": "forbid"}
    text: str = Field(..., description="Text to echo back verbatim.")


def get_time_tool() -> Tool:
    def _handler(_: _NoArgs) -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    return FunctionTool(
        name="get_time",
        description="Return the current UTC time as an ISO-8601 string.",
        input_model=_NoArgs,
        handler=_handler,
    )


def echo_tool() -> Tool:
    def _handler(args: _EchoArgs) -> str:
        return args.text

    return FunctionTool(
        name="echo",
        description="Echo the provided text back. Useful for tests.",
        input_model=_EchoArgs,
        handler=_handler,
    )


def default_tools() -> list[Tool]:
    """Built-in read-only tools every agent gets without opt-in.

    Mirrors openclaw's pi-coding-agent default bundle — `read`, `glob`,
    `grep`, `list_dir`, `read_pdf` are always safe to expose because
    they don't mutate state. Mutating tools (`write_file`, `edit`,
    `shell`, `process`) are added by `_build_default_tools` only when
    an `ApprovalManager` is injected, so the human-approval gate
    catches destructive calls on the default agent.
    """
    from oxenclaw.tools_pkg.fs_tools import (
        glob_tool,
        grep_tool,
        list_dir_tool,
        read_file_tool,
        read_pdf_tool,
    )

    return [
        get_time_tool(),
        echo_tool(),
        read_file_tool(),
        list_dir_tool(),
        grep_tool(),
        glob_tool(),
        read_pdf_tool(),
    ]
