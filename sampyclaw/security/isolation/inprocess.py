"""Inprocess backend: run argv directly via asyncio subprocess in this process group.

This is the weakest backend. It enforces ONLY a wall-clock timeout and
output truncation. Use only for tools you fully trust — chiefly the
no-isolation default for read-only built-ins. Stronger backends should be
preferred whenever they are available.
"""

from __future__ import annotations

import asyncio
import time

from sampyclaw.security.isolation._truncate import truncate
from sampyclaw.security.isolation.policy import IsolationPolicy, IsolationResult


class InprocessBackend:
    name = "inprocess"

    async def is_available(self) -> bool:
        return True

    async def run(
        self,
        argv: list[str],
        *,
        policy: IsolationPolicy,
        stdin: bytes | None = None,
        cwd: str | None = None,
    ) -> IsolationResult:
        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
        except FileNotFoundError as exc:
            return IsolationResult(
                backend="inprocess",
                exit_code=-1,
                stdout="",
                stderr="",
                duration_seconds=time.monotonic() - start,
                error=str(exc),
            )

        timed_out = False
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(input=stdin), timeout=policy.timeout_seconds
            )
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            try:
                out, err = await proc.communicate()
            except Exception:
                out, err = b"", b""
        duration = time.monotonic() - start
        stdout, t_out = truncate(out or b"", policy.max_output_bytes)
        stderr, t_err = truncate(err or b"", policy.max_output_bytes)
        return IsolationResult(
            backend="inprocess",
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            timed_out=timed_out,
            truncated_stdout=t_out,
            truncated_stderr=t_err,
        )
