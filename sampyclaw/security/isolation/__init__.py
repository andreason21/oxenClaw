"""Tool execution isolation backends.

Layered defenses, strongest-first:

1. **container** — `docker run` / `podman run` with `--network=none`,
   `--read-only`, capability drops, memory/cpu caps.
2. **bwrap** — bubblewrap mount-namespace + new user namespace; `tmpfs /tmp`,
   read-only bind of host root, optional `--share-net`.
3. **subprocess** — fresh process with `setrlimit(AS, CPU, FSIZE, NOFILE)`
   plus a hard wall-clock timeout. Output truncation.
4. **inprocess** — no isolation. Used only for explicitly-trusted Python
   tools where the cost of a fork is wasteful.

Callers describe their *intent* via `IsolationPolicy`; the backend registry
picks the strongest available backend that satisfies the policy and
gracefully falls back when stronger ones are missing.
"""

from sampyclaw.security.isolation.bwrap import BubblewrapBackend
from sampyclaw.security.isolation.container import ContainerBackend
from sampyclaw.security.isolation.inprocess import InprocessBackend
from sampyclaw.security.isolation.policy import (
    BackendName,
    Filesystem,
    IsolationPolicy,
    IsolationResult,
)
from sampyclaw.security.isolation.registry import (
    BackendRegistry,
    available_backends,
    default_registry,
    resolve_backend,
)
from sampyclaw.security.isolation.subprocess_backend import SubprocessBackend

__all__ = [
    "BackendName",
    "BackendRegistry",
    "BubblewrapBackend",
    "ContainerBackend",
    "Filesystem",
    "InprocessBackend",
    "IsolationPolicy",
    "IsolationResult",
    "SubprocessBackend",
    "available_backends",
    "default_registry",
    "resolve_backend",
]
