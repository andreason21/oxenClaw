"""Discover plugins via `importlib.metadata.entry_points`.

Third-party packages declare:

    [project.entry-points."oxenclaw.plugins"]
    my_channel = "my_package.plugin:ENTRY"

Where `ENTRY` is either a `PluginEntry` instance or a zero-arg callable
returning one. The callable form lets packages defer heavy imports until
their plugin is actually requested.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from oxenclaw.plugin_sdk.runtime_env import get_logger
from oxenclaw.plugins.registry import PluginEntry, PluginRegistry

logger = get_logger("plugins.discovery")

ENTRY_POINT_GROUP = "oxenclaw.plugins"


class _EntryPointLike(Protocol):
    name: str

    def load(self) -> Any: ...


def discover_plugins(
    *,
    entries: Iterable[_EntryPointLike] | None = None,
    registry: PluginRegistry | None = None,
) -> PluginRegistry:
    """Scan `oxenclaw.plugins` entry points and register each.

    `entries` may be supplied in tests; defaults to
    `importlib.metadata.entry_points(group=...)` in production. Bad entries
    are skipped with a warning — discovery must never explode the whole
    gateway startup because a single third-party package is broken.
    """
    registry = registry if registry is not None else PluginRegistry()

    if entries is None:
        from importlib.metadata import entry_points

        entries = entry_points(group=ENTRY_POINT_GROUP)

    for ep in entries:
        try:
            obj = ep.load()
        except Exception:
            logger.exception("plugin entry %r failed to load", getattr(ep, "name", "?"))
            continue

        entry = _materialise(obj)
        if entry is None:
            logger.warning(
                "plugin entry %r produced unexpected object: %r",
                getattr(ep, "name", "?"),
                obj,
            )
            continue

        try:
            registry.register(entry)
        except ValueError as exc:
            logger.warning("plugin register failed: %s", exc)

    return registry


def _materialise(obj: Any) -> PluginEntry | None:
    if isinstance(obj, PluginEntry):
        return obj
    if callable(obj):
        try:
            produced = obj()
        except Exception:
            logger.exception("plugin factory call failed")
            return None
        if isinstance(produced, PluginEntry):
            return produced
    return None
