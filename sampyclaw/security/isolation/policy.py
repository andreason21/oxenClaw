"""Isolation policy + result types shared across all backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

BackendName = Literal["inprocess", "subprocess", "bwrap", "container"]
Filesystem = Literal["none", "readonly", "scratch", "full"]


@dataclass(frozen=True)
class IsolationPolicy:
    """How aggressively to box in a single tool invocation.

    Defaults are deliberately strict — opt out, don't opt in.
    """

    timeout_seconds: float = 30.0
    max_output_bytes: int = 1 * 1024 * 1024  # 1 MiB

    # Resource caps — None means "leave to OS default" for backends that
    # don't enforce, but subprocess+bwrap will still cap at sane values.
    max_memory_mb: int | None = 512
    max_cpu_seconds: float | None = 30.0
    max_file_size_mb: int | None = 64
    max_open_files: int | None = 64
    # Process/thread cap — defends against fork-bombs. Applied via RLIMIT_NPROC.
    max_processes: int | None = 64
    # Maximum stdin payload bytes the caller may send. Refuse oversized input
    # before spawning the subprocess so a model-driven huge args dict can't
    # silently grow memory in the host. None disables the check.
    max_stdin_bytes: int | None = 4 * 1024 * 1024
    # Tmpfs sizes inside bwrap. Without explicit caps, kernel default (~½ RAM)
    # would let a sandboxed tool fill /tmp and DoS the host.
    tmpfs_size_mb: int = 64

    # Communication.
    network: bool = False  # default: deny.
    filesystem: Filesystem = "none"  # default: scratch tmpfs only.

    # Backend hint. None = pick strongest available.
    backend: BackendName | None = None

    # Container-specific.
    container_image: str = "alpine:3.20"
    container_runtime: Literal["docker", "podman"] | None = None  # auto-detect.

    # Allowlist of env vars to propagate from the host (always sanitised).
    env_passthrough: tuple[str, ...] = field(default_factory=tuple)
    # Literal env vars to inject into the child (NOT read from host env).
    # Used by IsolatedFunctionTool to set a curated PYTHONPATH that does not
    # leak the host's full site-packages into the sandbox.
    env_inject: tuple[tuple[str, str], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class IsolationResult:
    """Outcome of a single isolated invocation."""

    backend: BackendName
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    oom: bool = False
    truncated_stdout: bool = False
    truncated_stderr: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None


@runtime_checkable
class IsolationBackend(Protocol):
    """Run a shell command (argv list) under this backend's isolation."""

    name: BackendName

    async def is_available(self) -> bool: ...

    async def run(
        self,
        argv: list[str],
        *,
        policy: IsolationPolicy,
        stdin: bytes | None = None,
        cwd: str | None = None,
    ) -> IsolationResult: ...
