"""ShellTool + IsolatedFunctionTool tests."""

from __future__ import annotations

import sys

import pytest
from pydantic import BaseModel

from sampyclaw.agents.tools import Tool
from sampyclaw.security.isolated_function import IsolatedFunctionTool
from sampyclaw.security.isolation.policy import IsolationPolicy
from sampyclaw.security.shell_tool import (
    ShellTool,
    ShellToolError,
    ping_host_tool,
    python_snippet_tool,
)


class _Args(BaseModel):
    text: str


def _echo_tool() -> ShellTool:
    return ShellTool(
        name="echo_text",
        description="Echo text.",
        input_model=_Args,
        argv_template=["echo", "{text}"],
        policy=IsolationPolicy(backend="subprocess", timeout_seconds=2, network=True, filesystem="full"),
    )


def test_shell_tool_implements_tool_protocol() -> None:
    tool = _echo_tool()
    assert isinstance(tool, Tool)
    assert tool.name == "echo_text"
    assert "type" in tool.input_schema


def test_shell_tool_requires_argv_template() -> None:
    with pytest.raises(ValueError):
        ShellTool(
            name="x",
            description="x",
            input_model=_Args,
            argv_template=[],
        )


async def test_shell_tool_basic_echo() -> None:
    tool = _echo_tool()
    out = await tool.execute({"text": "hello world"})
    assert out.strip() == "hello world"


async def test_shell_tool_quotes_dangerous_args() -> None:
    """A `;` in the input must NOT execute a second command — it must be quoted."""
    if sys.platform == "win32":
        pytest.skip("POSIX shell semantics")
    tool = _echo_tool()
    out = await tool.execute({"text": "hi; echo PWNED"})
    # If quoting failed, "PWNED" would appear on its own line.
    assert "PWNED" in out  # echoed as literal text
    # But the literal `;` should be in the output too — it's part of the echo arg.
    assert ";" in out


async def test_shell_tool_timeout_raises() -> None:
    tool = ShellTool(
        name="slow",
        description="x",
        input_model=_Args,
        argv_template=["sleep", "5"],
        policy=IsolationPolicy(backend="subprocess", timeout_seconds=0.5, network=True, filesystem="full"),
    )
    with pytest.raises(ShellToolError, match="timed out"):
        await tool.execute({"text": "ignored"})


async def test_shell_tool_nonzero_exit_raises() -> None:
    tool = ShellTool(
        name="fail",
        description="x",
        input_model=_Args,
        argv_template=["false"],
        policy=IsolationPolicy(backend="subprocess", timeout_seconds=2, network=True, filesystem="full"),
    )
    with pytest.raises(ShellToolError):
        await tool.execute({"text": "ignored"})


async def test_python_snippet_tool_runs() -> None:
    """The default python_snippet policy is strict (network=False); requires
    a backend that can actually enforce that. On hosts without bwrap/container,
    fail-closed is the correct outcome — skip the live-run test."""
    from sampyclaw.security.isolation.registry import resolve_backend
    from sampyclaw.security.isolation.policy import IsolationPolicy as _Pol

    backend = await resolve_backend(_Pol(network=False, filesystem="none"))
    if backend.name == "subprocess":
        pytest.skip("no real-isolation backend available; subprocess fail-closed is correct")
    tool = python_snippet_tool()
    out = await tool.execute({"code": "print(1 + 2)"})
    assert out.strip() == "3"


async def test_python_snippet_tool_memory_capped() -> None:
    tool = python_snippet_tool(
        policy=IsolationPolicy(
            backend="subprocess",
            timeout_seconds=3,
            max_memory_mb=64,
            max_cpu_seconds=3,
            network=True,
            filesystem="full",
        )
    )
    with pytest.raises(ShellToolError):
        await tool.execute(
            {"code": "a = bytearray(256*1024*1024); print('escaped')"}
        )


def test_ping_host_tool_metadata() -> None:
    tool = ping_host_tool()
    assert tool.name == "ping_host"
    assert tool.policy.network is True
    assert "host" in tool.input_schema["properties"]


# ── IsolatedFunctionTool ──


async def test_isolated_function_tool_runs_in_subprocess() -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX-only")
    tool = IsolatedFunctionTool(
        name="iso_echo",
        description="echo via isolated subprocess",
        input_model=_Args,
        handler_path="tests._iso_test_handler:echo_args",
        policy=IsolationPolicy(backend="subprocess", timeout_seconds=10, max_memory_mb=128, network=True, filesystem="full"),
    )
    out = await tool.execute({"text": "hello"})
    assert out == "ok:hello"


async def test_isolated_function_tool_handles_sync_handler() -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX-only")
    tool = IsolatedFunctionTool(
        name="iso_sync",
        description="sync handler in isolated subprocess",
        input_model=_Args,
        handler_path="tests._iso_test_handler:sync_echo",
        policy=IsolationPolicy(backend="subprocess", timeout_seconds=10, max_memory_mb=128, network=True, filesystem="full"),
    )
    out = await tool.execute({"text": "world"})
    assert out == "sync:world"


async def test_isolated_function_tool_surfaces_handler_exceptions() -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX-only")
    tool = IsolatedFunctionTool(
        name="iso_boom",
        description="explode in isolated subprocess",
        input_model=_Args,
        handler_path="tests._iso_test_handler:boom",
        policy=IsolationPolicy(backend="subprocess", timeout_seconds=10, max_memory_mb=128, network=True, filesystem="full"),
    )
    with pytest.raises(ShellToolError, match="intentional failure"):
        await tool.execute({"text": "ignored"})


def test_isolated_function_tool_validates_handler_path() -> None:
    with pytest.raises(ValueError):
        IsolatedFunctionTool(
            name="bad",
            description="x",
            input_model=_Args,
            handler_path="no_colon_here",
        )
