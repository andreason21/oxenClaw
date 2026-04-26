"""Tests for discover_plugins() — entry-point loading path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from oxenclaw.plugins.discovery import discover_plugins
from oxenclaw.plugins.manifest import Manifest
from oxenclaw.plugins.registry import PluginEntry


@dataclass
class _FakeEntry:
    name: str
    target: Any

    def load(self) -> Any:
        return self.target


def _entry(plugin_id: str) -> PluginEntry:
    return PluginEntry(
        manifest=Manifest(id=plugin_id, channels=[plugin_id]),
        factory=lambda **kw: object(),
    )


def test_discovery_registers_instance_entries() -> None:
    a = _entry("a")
    b = _entry("b")
    reg = discover_plugins(entries=[_FakeEntry("a", a), _FakeEntry("b", b)])
    assert sorted(reg.ids()) == ["a", "b"]


def test_discovery_calls_callable_returning_entry() -> None:
    entry = _entry("lazy")

    def _factory() -> PluginEntry:
        return entry

    reg = discover_plugins(entries=[_FakeEntry("lazy", _factory)])
    assert "lazy" in reg


def test_discovery_skips_broken_load() -> None:
    class _Boom:
        name = "boom"

        def load(self) -> Any:
            raise RuntimeError("import failed")

    good = _entry("good")
    reg = discover_plugins(entries=[_Boom(), _FakeEntry("good", good)])
    assert reg.ids() == ["good"]


def test_discovery_skips_non_plugin_targets() -> None:
    reg = discover_plugins(entries=[_FakeEntry("weird", 42)])
    assert len(reg) == 0


def test_discovery_skips_broken_factory() -> None:
    def _boom() -> PluginEntry:
        raise RuntimeError("factory bomb")

    reg = discover_plugins(entries=[_FakeEntry("boom", _boom)])
    assert len(reg) == 0


def test_discovery_skips_duplicate_without_failing() -> None:
    a = _entry("dup")
    reg = discover_plugins(entries=[_FakeEntry("a", a), _FakeEntry("b", a)])
    assert reg.ids() == ["dup"]  # only one registered; second is warned + dropped


def test_discovery_real_entry_points_group_available() -> None:
    """Smoke-test: the production default (no `entries` kwarg) at least runs.

    The installed entry-points table may be empty depending on how the package
    is installed; we only verify the call doesn't explode.
    """
    reg = discover_plugins()
    assert isinstance(reg.ids(), list)
