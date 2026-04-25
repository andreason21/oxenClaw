"""Standalone handler module used by isolated-function tests.

Lives outside `test_shell_tool.py` because the subprocess imports its
declaring module, and `test_shell_tool.py` pulls in pytest — which the
isolated subprocess wouldn't have on a stripped env.
"""

from __future__ import annotations


async def echo_args(args: dict) -> str:  # type: ignore[type-arg]
    return f"ok:{args.get('text', '')}"


def sync_echo(args: dict) -> str:  # type: ignore[type-arg]
    return f"sync:{args.get('text', '')}"


async def boom(args: dict) -> str:  # type: ignore[type-arg]
    raise RuntimeError("intentional failure for tests")
