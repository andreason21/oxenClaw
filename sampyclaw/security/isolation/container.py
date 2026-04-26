"""Docker / Podman backend — strongest available isolation.

Auto-detects `docker` then `podman`. Runs the inner command in:

  docker run --rm \
    --network=none           (or omitted when policy.network=True)
    --read-only --tmpfs /tmp:rw --tmpfs /work:rw --workdir /work
    --memory=Nm --cpus=N --pids-limit=64
    --cap-drop=ALL --security-opt=no-new-privileges
    --user 65534:65534
    <image> <argv...>

The default image is `alpine:3.20`. Callers can override per-policy.
For short-lived shell tools this is significantly heavier than bwrap
(~200ms cold-start) but offers the strongest practical isolation.
"""

from __future__ import annotations

import asyncio
import shutil
import time

from sampyclaw.security.isolation._truncate import truncate
from sampyclaw.security.isolation.policy import IsolationPolicy, IsolationResult

_RUNTIME_CACHE: tuple[str | None, str | None] = (None, None)


def _detect_runtime(prefer: str | None) -> str | None:
    candidates: list[str]
    if prefer == "docker":
        candidates = ["docker"]
    elif prefer == "podman":
        candidates = ["podman"]
    else:
        candidates = ["docker", "podman"]
    for c in candidates:
        path = shutil.which(c)
        if path:
            return path
    return None


def _build_container_argv(
    runtime: str, inner_argv: list[str], *, policy: IsolationPolicy
) -> list[str]:
    args: list[str] = [
        runtime,
        "run",
        "--rm",
        "-i",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=64m",
        "--tmpfs",
        "/work:rw,size=64m",
        "--workdir",
        "/work",
        "--cap-drop=ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--user",
        "65534:65534",
        "--pids-limit",
        "128",
    ]
    if not policy.network:
        args += ["--network=none"]
    if policy.max_memory_mb is not None:
        args += [f"--memory={policy.max_memory_mb}m"]
    if policy.max_cpu_seconds is not None:
        # cpus=fraction for total wall-CPU; rough analogue.
        args += [
            f"--cpus={max(0.1, policy.max_cpu_seconds / max(policy.timeout_seconds, 0.1)):.2f}"
        ]
    args.append(policy.container_image)
    args.extend(inner_argv)
    return args


class ContainerBackend:
    name = "container"

    async def is_available(self) -> bool:
        return _detect_runtime(None) is not None

    async def run(
        self,
        argv: list[str],
        *,
        policy: IsolationPolicy,
        stdin: bytes | None = None,
        cwd: str | None = None,
    ) -> IsolationResult:
        runtime = _detect_runtime(policy.container_runtime)
        if runtime is None:
            return IsolationResult(
                backend="container",
                exit_code=-1,
                stdout="",
                stderr="",
                duration_seconds=0.0,
                error="no docker/podman runtime found",
            )
        full_argv = _build_container_argv(runtime, argv, policy=policy)
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *full_argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
        )
        timed_out = False
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(input=stdin), timeout=policy.timeout_seconds
            )
        except TimeoutError:
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
            backend="container",
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            timed_out=timed_out,
            truncated_stdout=t_out,
            truncated_stderr=t_err,
        )
