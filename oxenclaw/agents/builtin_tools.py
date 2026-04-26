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
    return [get_time_tool(), echo_tool()]
