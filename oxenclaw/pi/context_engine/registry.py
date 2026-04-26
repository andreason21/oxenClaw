"""Owner-keyed ContextEngine registry.

Mirrors openclaw `src/context-engine/registry.ts`. Slot ids ("legacy",
"active-memory", etc.) are configured in `config.yaml`; plugins
register a factory keyed by the owner id (their plugin id) so the host
can refresh / replace registrations idempotently.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Union

from oxenclaw.pi.context_engine.types import ContextEngine

logger = logging.getLogger(__name__)

# A factory may be sync or async — both are accepted.
ContextEngineFactory = Callable[[], Union[ContextEngine, Awaitable[ContextEngine]]]


@dataclass
class ContextEngineRegistrationResult:
    ok: bool
    existing_owner: str | None = None


# Slot id → (owner id, factory).
_REGISTRY: dict[str, tuple[str, ContextEngineFactory]] = {}
_INITIALIZED = False


def register_context_engine_for_owner(
    *,
    slot: str,
    owner: str,
    factory: ContextEngineFactory,
    allow_same_owner_refresh: bool = True,
) -> ContextEngineRegistrationResult:
    """Register `factory` under `slot`, owned by `owner`.

    A second registration for the same slot from a *different* owner is
    refused (returns `ok=False`); the same owner re-registering is
    allowed by default so plugin reloads pick up the new factory.
    """
    existing = _REGISTRY.get(slot)
    if existing is not None and existing[0] != owner:
        return ContextEngineRegistrationResult(ok=False, existing_owner=existing[0])
    if existing is not None and not allow_same_owner_refresh:
        return ContextEngineRegistrationResult(ok=False, existing_owner=existing[0])
    _REGISTRY[slot] = (owner, factory)
    return ContextEngineRegistrationResult(ok=True)


def register_context_engine(
    *,
    slot: str,
    factory: ContextEngineFactory,
    owner: str = "host",
) -> ContextEngineRegistrationResult:
    """Convenience wrapper for the host-default registration path."""
    return register_context_engine_for_owner(slot=slot, owner=owner, factory=factory)


async def resolve_context_engine(slot: str = "legacy") -> ContextEngine | None:
    """Instantiate the engine registered under `slot`, or None.

    The factory may be sync or async. The default `slot="legacy"` always
    resolves once `ensure_context_engines_initialized()` has run.
    """
    entry = _REGISTRY.get(slot)
    if entry is None:
        return None
    _, factory = entry
    result = factory()
    if asyncio.iscoroutine(result):
        return await result  # type: ignore[no-any-return]
    return result  # type: ignore[return-value]


def ensure_context_engines_initialized() -> None:
    """Idempotently register the legacy engine as a fallback for `slot="legacy"`.

    Mirrors openclaw `init.ts:ensureContextEnginesInitialized()`. Called
    once at gateway boot; safe to call repeatedly.
    """
    global _INITIALIZED
    if _INITIALIZED:
        return
    _INITIALIZED = True
    # Imported here to avoid a module-level cycle (legacy uses types
    # which uses the registry only at runtime).
    from oxenclaw.pi.context_engine.legacy import register_legacy_context_engine

    register_legacy_context_engine()


def _reset_for_tests() -> None:
    """Test-only: clear the registry. Tests that pin specific
    registrations call this in a fixture."""
    global _INITIALIZED
    _REGISTRY.clear()
    _INITIALIZED = False


def list_slots() -> list[str]:
    """Return the slot ids that currently have a registration.

    Useful for the dashboard / `oxenclaw doctor` to display which
    engines are wired up.
    """
    return sorted(_REGISTRY.keys())


def clear_context_engines_for_owner(owner: str) -> list[str]:
    """Drop every registration owned by `owner`. Returns the slot ids
    that were removed.

    Mirrors openclaw `clearContextEnginesForOwner`. Plugins call this
    on unload so a re-loaded plugin doesn't double-register.
    """
    cleared = [slot for slot, (current_owner, _) in _REGISTRY.items() if current_owner == owner]
    for slot in cleared:
        _REGISTRY.pop(slot, None)
    return cleared


def get_context_engine_factory(slot: str) -> ContextEngineFactory | None:
    """Return the raw factory for `slot` without instantiating.

    Counterpart to `resolve_context_engine`, which calls the factory.
    Useful for callers (tests, lifecycle hooks) that need to inspect
    or wrap the factory without paying instantiation cost.
    """
    entry = _REGISTRY.get(slot)
    return entry[1] if entry is not None else None


__all__ = [
    "ContextEngineFactory",
    "ContextEngineRegistrationResult",
    "clear_context_engines_for_owner",
    "ensure_context_engines_initialized",
    "get_context_engine_factory",
    "list_slots",
    "register_context_engine",
    "register_context_engine_for_owner",
    "resolve_context_engine",
]
