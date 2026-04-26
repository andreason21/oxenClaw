"""ShellTool — a Tool that runs a shell command under isolation.

The command is built from an argv template plus per-arg `shlex.quote` to
prevent injection. It implements the `Tool` Protocol so any tool registry
can hold one.

Always runs through the isolation backend resolved from `policy`. There
is no in-process escape hatch — if you trust your code that much, use
FunctionTool instead.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from oxenclaw.agents.tools import Tool
from oxenclaw.security.isolation.policy import IsolationPolicy, IsolationResult
from oxenclaw.security.isolation.registry import resolve_backend


class ShellToolError(RuntimeError):
    """Raised when a shell-tool invocation fails (non-zero exit, timeout, ...)."""

    def __init__(self, message: str, *, result: IsolationResult) -> None:
        super().__init__(message)
        self.result = result


def _format_argv_template(template: list[str], args: dict[str, Any]) -> list[str]:
    """Substitute named placeholders in `template` with raw arg values.

    Argv-exec is intrinsically immune to shell injection (each entry is a
    single token, never re-parsed by a shell), so we substitute values
    *without* shell quoting — wrapping in single quotes would have those
    quotes show up literally in the executed argument.

    Tools that need shell semantics (e.g. wildcards, redirection) should
    explicitly use `["sh", "-c", "...{name}..."]` form and apply
    `shlex.quote` themselves inside the template — that's a deliberate
    opt-in. Most tools should stick to argv form.
    """

    class _NamespaceDict(dict):  # type: ignore[type-arg]
        def __missing__(self, key: str) -> str:
            raise KeyError(f"missing argument: {key!r}")

    ns = _NamespaceDict({k: str(v) for k, v in args.items()})
    return [item.format_map(ns) for item in template]


class ShellTool:
    """Tool that runs a shell command via the isolation registry.

    `argv_template`: list of argv entries with `{name}` placeholders that
    map to keys in the input model. Each substitution is shlex-quoted.

    Example:

        ShellTool(
            name="ping_host",
            description="Ping a host once.",
            input_model=PingArgs,
            argv_template=["ping", "-c", "1", "-W", "2", "{host}"],
            policy=IsolationPolicy(network=True, timeout_seconds=5),
        )
    """

    def __init__(
        self,
        *,
        name: str,
        description: str,
        input_model: type[BaseModel],
        argv_template: list[str],
        policy: IsolationPolicy | None = None,
    ) -> None:
        if not name:
            raise ValueError("name is required")
        if not argv_template:
            raise ValueError("argv_template must be non-empty")
        self.name = name
        self.description = description
        self._input_model = input_model
        self._argv_template = list(argv_template)
        self._policy = policy or IsolationPolicy()

    @property
    def input_schema(self) -> dict[str, Any]:
        return self._input_model.model_json_schema()

    @property
    def policy(self) -> IsolationPolicy:
        return self._policy

    async def execute(self, args: dict[str, Any]) -> str:
        parsed = self._input_model.model_validate(args)
        argv = _format_argv_template(self._argv_template, parsed.model_dump())
        backend = await resolve_backend(self._policy)
        result = await backend.run(argv, policy=self._policy)
        if result.timed_out:
            raise ShellToolError(
                f"shell tool {self.name!r} timed out after {self._policy.timeout_seconds}s",
                result=result,
            )
        if result.error is not None:
            raise ShellToolError(f"shell tool {self.name!r} failed: {result.error}", result=result)
        if result.exit_code != 0:
            tail = result.stderr.strip().splitlines()[-3:] if result.stderr else []
            raise ShellToolError(
                f"shell tool {self.name!r} exited {result.exit_code}: "
                + (" / ".join(tail) if tail else result.stdout[:200]),
                result=result,
            )
        return result.stdout


# ── Built-in safe shell tools ────────────────────────────────────────────


class _PingArgs(BaseModel):
    host: str


def ping_host_tool() -> ShellTool:
    return ShellTool(
        name="ping_host",
        description="Send a single ICMP echo to `host` (2s timeout). Returns the ping output.",
        input_model=_PingArgs,
        argv_template=["ping", "-c", "1", "-W", "2", "{host}"],
        policy=IsolationPolicy(network=True, timeout_seconds=5.0, max_memory_mb=64),
    )


class _PythonSnippetArgs(BaseModel):
    code: str


def python_snippet_tool(*, policy: IsolationPolicy | None = None) -> ShellTool:
    """Run a *short* Python snippet inside isolation. No network, 5s timeout, 128MB.

    The snippet is passed via `python3 -c <code>`. Output is whatever the
    snippet prints. This is useful for the agent to do small calculations
    safely; for anything substantive the agent should ask the user to
    compose a proper tool.
    """
    return ShellTool(
        name="python_snippet",
        description=(
            "Run a short Python snippet in a sandboxed subprocess (no network, "
            "5s wall-clock, 128 MiB memory). Returns the captured stdout."
        ),
        input_model=_PythonSnippetArgs,
        argv_template=["python3", "-c", "{code}"],
        policy=policy
        or IsolationPolicy(
            network=False,
            timeout_seconds=5.0,
            max_memory_mb=128,
            max_cpu_seconds=5.0,
        ),
    )


def safe_shell_tools() -> list[Tool]:
    """Curated tiny set of shell-backed tools that's safe to register by default."""
    out: list[Tool] = []
    out.append(ping_host_tool())
    out.append(python_snippet_tool())
    return out


__all__ = [
    "ShellTool",
    "ShellToolError",
    "ping_host_tool",
    "python_snippet_tool",
    "safe_shell_tools",
]
