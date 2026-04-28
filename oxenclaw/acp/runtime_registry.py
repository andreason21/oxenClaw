"""ACP backend registry — pluggable `AcpRuntime` implementations.

Mirrors openclaw `src/acp/runtime/registry.ts:6-115`. Backends register
by id; the manager resolves a backend by id when a session is opened.

Single module-level dict; thread-safe enough because Python's GIL
serialises dict mutation. For pytest isolation, call
`reset_for_tests()` between cases.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from oxenclaw.agents.acp_runtime import AcpRuntime


class AcpRegistryError(Exception):
    """Raised when a backend cannot be registered or resolved."""


@dataclass(frozen=True)
class AcpRuntimeBackend:
    id: str
    runtime: AcpRuntime
    healthy: Callable[[], bool] | None = None


_BACKENDS: dict[str, AcpRuntimeBackend] = {}


def _normalise_id(raw: str | None) -> str:
    return (raw or "").strip().lower()


def register_acp_runtime_backend(backend: AcpRuntimeBackend) -> None:
    """Add or replace a backend by id (case-insensitive)."""
    bid = _normalise_id(backend.id)
    if not bid:
        raise AcpRegistryError("ACP runtime backend id is required")
    if backend.runtime is None:
        raise AcpRegistryError(f"ACP runtime backend {bid!r} is missing runtime implementation")
    _BACKENDS[bid] = AcpRuntimeBackend(id=bid, runtime=backend.runtime, healthy=backend.healthy)


def unregister_acp_runtime_backend(backend_id: str) -> None:
    """Drop a backend by id (no-op if unknown)."""
    _BACKENDS.pop(_normalise_id(backend_id), None)


def _is_healthy(backend: AcpRuntimeBackend) -> bool:
    if backend.healthy is None:
        return True
    try:
        return bool(backend.healthy())
    except Exception:  # pragma: no cover — guard
        return False


def get_acp_runtime_backend(
    backend_id: str | None = None,
) -> AcpRuntimeBackend | None:
    """Resolve a backend by id, or return *any* healthy backend if id omitted.

    Mirrors openclaw `getAcpRuntimeBackend`: when no id is supplied,
    fall through the registered backends in insertion order and pick
    the first healthy one. Returns None when nothing is registered or
    the requested id is not present.
    """
    bid = _normalise_id(backend_id) if backend_id else ""
    if bid:
        return _BACKENDS.get(bid)
    if not _BACKENDS:
        return None
    for backend in _BACKENDS.values():
        if _is_healthy(backend):
            return backend
    return None


def require_acp_runtime_backend(backend_id: str | None = None) -> AcpRuntimeBackend:
    """Like `get_acp_runtime_backend` but raises if nothing matches."""
    backend = get_acp_runtime_backend(backend_id)
    if backend is None:
        if backend_id:
            raise AcpRegistryError(f"ACP runtime backend {backend_id!r} is not registered")
        raise AcpRegistryError("no ACP runtime backends registered")
    return backend


def list_acp_runtime_backends() -> list[str]:
    """Snapshot of registered backend ids in insertion order."""
    return list(_BACKENDS.keys())


def reset_for_tests() -> None:
    """Clear all registrations. Test-only — production code must not call."""
    _BACKENDS.clear()


__all__ = [
    "AcpRegistryError",
    "AcpRuntimeBackend",
    "get_acp_runtime_backend",
    "list_acp_runtime_backends",
    "register_acp_runtime_backend",
    "require_acp_runtime_backend",
    "reset_for_tests",
    "unregister_acp_runtime_backend",
]
