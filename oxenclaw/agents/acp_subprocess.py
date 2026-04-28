"""ACP subprocess track — one-shot CLI invocation, no real ACP wire.

This is the *subprocess* track of the ACP harness — it runs an
external AI CLI as a one-shot child process and returns its captured
stdout once it exits. There is no NDJSON framing, no JSON-RPC, no
session, no streaming. The companion `acp_runtime.py` defines the
real protocol surface (the `AcpRuntime` Protocol) that future
backends will implement.

Three external runtimes are supported here:

  - **claude** — Anthropic's `claude` CLI (Claude Code).
  - **codex**  — OpenAI's `codex` CLI.
  - **gemini** — Google's `gemini` CLI.

The current scope covers "use a stronger external model for one
sub-task" without spinning up a long-running ACP server. Persistent
ACP sessions arrive via the runtime track.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from typing import Literal

from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("agents.acp_subprocess")

AcpRuntimeId = Literal["claude", "codex", "gemini"]


# CLI binary names per runtime — operators can override per-call.
_CLI_BY_RUNTIME: dict[AcpRuntimeId, str] = {
    "claude": "claude",
    "codex": "codex",
    "gemini": "gemini",
}


@dataclass
class AcpSpawnResult:
    """Outcome of one ACP-CLI invocation."""

    runtime: AcpRuntimeId
    cli: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    error: str | None = None
    argv: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None


def resolve_cli(runtime: AcpRuntimeId, override: str | None = None) -> str | None:
    """Return absolute path to the CLI binary, or None if not installed."""
    name = override or _CLI_BY_RUNTIME.get(runtime, runtime)
    return shutil.which(name)


async def spawn_acp(
    *,
    runtime: AcpRuntimeId,
    prompt: str,
    cli_override: str | None = None,
    extra_args: list[str] | None = None,
    cwd: str | None = None,
    timeout_seconds: float = 120.0,
    stdin_mode: Literal["arg", "stdin"] = "arg",
) -> AcpSpawnResult:
    """Run an external AI CLI once with `prompt` and capture its output.

    `stdin_mode='arg'` (default) passes the prompt as a positional
    argument — what `claude` and `codex` accept. `stdin_mode='stdin'`
    pipes the prompt to stdin instead — for CLIs that read from
    stdin (older `gemini` versions).

    Returns a structured `AcpSpawnResult` with stdout/stderr/exit
    code and a flag for timeouts. Operators wanting persistent
    bidirectional sessions should look at openclaw's `acp-spawn-
    parent-stream.ts`; that's a future port.
    """
    binary = resolve_cli(runtime, cli_override)
    if binary is None:
        return AcpSpawnResult(
            runtime=runtime,
            cli=cli_override or _CLI_BY_RUNTIME.get(runtime, runtime),
            exit_code=-1,
            stdout="",
            stderr="",
            duration_seconds=0.0,
            error=(
                f"CLI for runtime {runtime!r} not found on PATH "
                f"(looked for {cli_override or _CLI_BY_RUNTIME.get(runtime, runtime)!r}). "
                "Install it or set the override."
            ),
        )
    argv: list[str] = [binary]
    if extra_args:
        argv.extend(extra_args)
    if stdin_mode == "arg":
        argv.append(prompt)
        stdin_payload: bytes | None = None
    else:
        stdin_payload = prompt.encode("utf-8")
    started = asyncio.get_event_loop().time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin_payload is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
    except OSError as exc:
        return AcpSpawnResult(
            runtime=runtime,
            cli=binary,
            exit_code=-1,
            stdout="",
            stderr="",
            duration_seconds=0.0,
            error=f"failed to spawn {binary}: {exc}",
            argv=argv,
        )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(stdin_payload),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        elapsed = asyncio.get_event_loop().time() - started
        logger.warning(
            "acp_subprocess timeout: runtime=%s cli=%s timeout_s=%.1f",
            runtime,
            binary,
            timeout_seconds,
        )
        return AcpSpawnResult(
            runtime=runtime,
            cli=binary,
            exit_code=-1,
            stdout="",
            stderr="",
            duration_seconds=elapsed,
            timed_out=True,
            argv=argv,
        )
    elapsed = asyncio.get_event_loop().time() - started
    return AcpSpawnResult(
        runtime=runtime,
        cli=binary,
        exit_code=proc.returncode or 0,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        duration_seconds=elapsed,
        argv=argv,
    )


__all__ = ["AcpRuntimeId", "AcpSpawnResult", "resolve_cli", "spawn_acp"]
