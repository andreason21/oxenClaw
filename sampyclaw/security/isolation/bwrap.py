"""Bubblewrap backend.

Wraps the subprocess in a `bwrap` invocation that creates a fresh user +
mount namespace with:

  - read-only bind of `/usr`, `/bin`, `/lib`, `/lib64`, `/etc`
  - tmpfs over `/tmp` and `/home`
  - `--unshare-all` (mount/pid/uts/cgroup/net by default)
  - optional `--share-net` if `policy.network` is True
  - `--die-with-parent` so an orphan can't outlive us

`bwrap` ships with most distros (it's a flatpak dependency). When it's
not installed, `is_available()` returns False and the registry falls back
to SubprocessBackend.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time

from sampyclaw.security.isolation._truncate import truncate
from sampyclaw.security.isolation.policy import IsolationPolicy, IsolationResult
from sampyclaw.security.isolation.subprocess_backend import (
    _make_preexec,
    _scrub_env,
)


_UNSET = object()
_BWRAP_PATH_CACHE: object | str | None = _UNSET


def _bwrap_path() -> str | None:
    global _BWRAP_PATH_CACHE
    if _BWRAP_PATH_CACHE is _UNSET:
        _BWRAP_PATH_CACHE = shutil.which("bwrap")
    return _BWRAP_PATH_CACHE  # type: ignore[return-value]


def _build_bwrap_argv(
    bwrap: str, inner_argv: list[str], *, policy: IsolationPolicy, cwd: str | None
) -> list[str]:
    args: list[str] = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--cap-drop",
        "ALL",
        # Minimal read-only host bind for libc + binaries.
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind-try",
        "/bin",
        "/bin",
        "--ro-bind-try",
        "/sbin",
        "/sbin",
        "--ro-bind-try",
        "/lib",
        "/lib",
        "--ro-bind-try",
        "/lib64",
        "/lib64",
        "--ro-bind-try",
        "/etc",
        "/etc",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        # Tmpfs size caps prevent a sandboxed tool from writing until host RAM
        # exhaustion. `--size=` accepts bytes; passed in MiB via policy.
        "--size",
        str(policy.tmpfs_size_mb * 1024 * 1024),
        "--tmpfs",
        "/tmp",
        "--size",
        str(policy.tmpfs_size_mb * 1024 * 1024),
        "--tmpfs",
        "/home",
        "--chdir",
        cwd or "/tmp",
    ]
    if policy.network:
        args.append("--share-net")
    args.append("--")
    args.extend(inner_argv)
    return args


class BubblewrapBackend:
    name = "bwrap"

    async def is_available(self) -> bool:
        return _bwrap_path() is not None

    async def run(
        self,
        argv: list[str],
        *,
        policy: IsolationPolicy,
        stdin: bytes | None = None,
        cwd: str | None = None,
    ) -> IsolationResult:
        bwrap = _bwrap_path()
        if bwrap is None:
            return IsolationResult(
                backend="bwrap",
                exit_code=-1,
                stdout="",
                stderr="",
                duration_seconds=0.0,
                error="bwrap not installed on this host",
            )
        full_argv = _build_bwrap_argv(bwrap, argv, policy=policy, cwd=cwd)
        start = time.monotonic()
        if (
            stdin is not None
            and policy.max_stdin_bytes is not None
            and len(stdin) > policy.max_stdin_bytes
        ):
            return IsolationResult(
                backend="bwrap",
                exit_code=-1,
                stdout="",
                stderr="",
                duration_seconds=time.monotonic() - start,
                error=(
                    f"stdin payload {len(stdin)}B exceeds policy.max_stdin_bytes="
                    f"{policy.max_stdin_bytes}"
                ),
            )
        proc = await asyncio.create_subprocess_exec(
            *full_argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_scrub_env(policy),
            preexec_fn=_make_preexec(policy),
            close_fds=True,
            start_new_session=True,
        )
        timed_out = False
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(input=stdin), timeout=policy.timeout_seconds
            )
        except asyncio.TimeoutError:
            timed_out = True
            try:
                if proc.pid is not None:
                    os.killpg(proc.pid, 9)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                out, err = await proc.communicate()
            except Exception:
                out, err = b"", b""
        duration = time.monotonic() - start
        stdout, t_out = truncate(out or b"", policy.max_output_bytes)
        stderr, t_err = truncate(err or b"", policy.max_output_bytes)
        exit_code = proc.returncode if proc.returncode is not None else -1
        return IsolationResult(
            backend="bwrap",
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            timed_out=timed_out,
            oom=exit_code in (-9, 137) and not timed_out and policy.max_memory_mb is not None,
            truncated_stdout=t_out,
            truncated_stderr=t_err,
        )
