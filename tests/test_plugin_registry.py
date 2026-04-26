"""Tests for PluginEntry + PluginRegistry."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from oxenclaw.plugins.manifest import Manifest
from oxenclaw.plugins.registry import PluginEntry, PluginRegistry


def _entry(plugin_id: str = "x") -> PluginEntry:
    manifest = Manifest(id=plugin_id, channels=[plugin_id])
    return PluginEntry(manifest=manifest, factory=lambda **kw: MagicMock())


def test_register_and_lookup() -> None:
    reg = PluginRegistry()
    e = _entry()
    reg.register(e)
    assert reg.get("x") is e
    assert reg.require("x") is e
    assert reg.ids() == ["x"]
    assert "x" in reg
    assert len(reg) == 1


def test_register_duplicate_raises() -> None:
    reg = PluginRegistry()
    reg.register(_entry())
    with pytest.raises(ValueError):
        reg.register(_entry())


def test_require_missing_raises() -> None:
    reg = PluginRegistry()
    with pytest.raises(KeyError):
        reg.require("nope")


def test_entry_create_delegates_to_factory() -> None:
    made = MagicMock()
    factory = MagicMock(return_value=made)
    e = PluginEntry(
        manifest=Manifest(id="x", channels=["x"]),
        factory=factory,
    )
    result = e.create(account_id="main", token="abc")
    assert result is made
    factory.assert_called_once_with(account_id="main", token="abc")


def test_manifests_returns_all() -> None:
    reg = PluginRegistry()
    reg.register(_entry("a"))
    reg.register(_entry("b"))
    ids = [m.id for m in reg.manifests()]
    assert sorted(ids) == ["a", "b"]
