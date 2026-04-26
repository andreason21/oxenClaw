"""Subprocess + setrlimit backend (Linux/POSIX).

Forks a fresh process with `RLIMIT_AS`, `RLIMIT_CPU`, `RLIMIT_FSIZE`,
`RLIMIT_NOFILE` enforced before exec. Wall-clock timeout via
`asyncio.wait_for`. Network is *not* blocked here — for that, use the
bwrap or container backend.

Available on any POSIX. Falls back gracefully on Windows (`is_available`
returns False).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time

from sampyclaw.plugin_sdk.runtime_env import get_logger
from sampyclaw.security.isolation._truncate import truncate
from sampyclaw.security.isolation.policy import IsolationPolicy, IsolationResult

logger = get_logger("security.isolation.subprocess")


def _make_preexec(policy: IsolationPolicy):  # type: ignore[no-untyped-def]
    """Build a preexec_fn that applies setrlimit caps in the child."""

    def _apply() -> None:
        import resource

        # Address space (virtual memory) cap.
        if policy.max_memory_mb is not None:
            bytes_cap = policy.max_memory_mb * 1024 * 1024
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(resource.RLIMIT_AS, (bytes_cap, bytes_cap))
        # CPU-seconds cap (SIGXCPU at soft, SIGKILL at hard).
        if policy.max_cpu_seconds is not None:
            secs = int(policy.max_cpu_seconds)
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(resource.RLIMIT_CPU, (secs, secs + 1))
        # File-size cap.
        if policy.max_file_size_mb is not None:
            fs = policy.max_file_size_mb * 1024 * 1024
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(resource.RLIMIT_FSIZE, (fs, fs))
        # Open-files cap.
        if policy.max_open_files is not None:
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(
                    resource.RLIMIT_NOFILE,
                    (policy.max_open_files, policy.max_open_files),
                )
        # Process/thread cap. Bwrap's pid-namespace already isolates this
        # somewhat; subprocess cannot, so cap matters here most.
        if policy.max_processes is not None:
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(
                    resource.RLIMIT_NPROC,
                    (policy.max_processes, policy.max_processes),
                )
        # Detach into its own process group so we can clean up reliably.
        with contextlib.suppress(OSError):
            os.setsid()

    return _apply


def _scrub_env(policy: IsolationPolicy) -> dict[str, str]:
    """Return a minimal env, propagating only allowlisted vars + injected vars."""
    base = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    for key in policy.env_passthrough:
        v = os.environ.get(key)
        if v is not None:
            base[key] = v
    # `env_inject` overrides passthrough — caller-supplied literal values.
    for key, value in policy.env_inject:
        base[key] = value
    return base


class SubprocessBackend:
    name = "subprocess"

    async def is_available(self) -> bool:
        return sys.platform != "win32"

    async def run(
        self,
        argv: list[str],
        *,
        policy: IsolationPolicy,
        stdin: bytes | None = None,
        cwd: str | None = None,
    ) -> IsolationResult:
        start = time.monotonic()
        # Fail-closed: this backend cannot enforce network/filesystem isolation.
        # Refuse to run if the policy demands either, rather than silently
        # giving the caller a sandbox that doesn't sandbox.
        if not policy.network:
            return IsolationResult(
                backend="subprocess",
                exit_code=-1,
                stdout="",
                stderr="",
                duration_seconds=time.monotonic() - start,
                error=(
                    "subprocess backend cannot enforce network=False; "
                    "install bwrap or use the container backend"
                ),
            )
        if policy.filesystem != "full":
            return IsolationResult(
                backend="subprocess",
                exit_code=-1,
                stdout="",
                stderr="",
                duration_seconds=time.monotonic() - start,
                error=(
                    f"subprocess backend cannot enforce filesystem={policy.filesystem!r}; "
                    "install bwrap or use the container backend"
                ),
            )
        if (
            stdin is not None
            and policy.max_stdin_bytes is not None
            and len(stdin) > policy.max_stdin_bytes
        ):
            return IsolationResult(
                backend="subprocess",
                exit_code=-1,
                stdout="",
                stderr="",
                duration_seconds=time.monotonic() - start,
                error=(
                    f"stdin payload {len(stdin)}B exceeds policy.max_stdin_bytes="
                    f"{policy.max_stdin_bytes}"
                ),
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=_scrub_env(policy),
                preexec_fn=_make_preexec(policy),
                close_fds=True,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            return IsolationResult(
                backend="subprocess",
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
        except TimeoutError:
            timed_out = True
            try:
                # Kill the entire process group (we used setsid).
                if proc.pid is not None:
                    os.killpg(proc.pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                out, err = await proc.communicate()
            except Exception:
                out, err = b"", b""
        duration = time.monotonic() - start

        # Detect OOM via signal (SIGKILL). Heuristic — kernel doesn't tell us
        # explicitly, but exit_code == -9 / 137 with empty stdout is telling.
        exit_code = proc.returncode if proc.returncode is not None else -1
        oom = policy.max_memory_mb is not None and exit_code in (-9, 137) and not timed_out

        stdout, t_out = truncate(out or b"", policy.max_output_bytes)
        stderr, t_err = truncate(err or b"", policy.max_output_bytes)
        return IsolationResult(
            backend="subprocess",
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            timed_out=timed_out,
            oom=oom,
            truncated_stdout=t_out,
            truncated_stderr=t_err,
        )
