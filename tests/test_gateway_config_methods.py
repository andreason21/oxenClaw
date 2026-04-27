"""Tests for config.get / config.reload RPCs."""

from __future__ import annotations

from oxenclaw.gateway.config_methods import register_config_methods
from oxenclaw.gateway.router import Router
from oxenclaw.plugin_sdk.config_schema import RootConfig


async def test_get_empty_when_no_config(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    router = Router()
    register_config_methods(router)
    resp = await router.dispatch({"jsonrpc": "2.0", "id": 1, "method": "config.get"})
    # M-8 added a `memory.privacy` block to the default RootConfig.
    assert resp.result["channels"] == {}
    assert resp.result["providers"] == {}
    assert resp.result["agents"] == {}
    assert resp.result["clawhub"] is None
    assert resp.result["memory"]["privacy"]["redact_level"] == "light"


async def test_reload_reads_written_file(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("channels:\n  dashboard:\n    dm_policy: open\n")
    router = Router()
    register_config_methods(router)
    resp = await router.dispatch({"jsonrpc": "2.0", "id": 1, "method": "config.reload"})
    assert resp.result["reloaded"] is True
    assert resp.result["channels"] == ["dashboard"]


async def test_reload_invokes_sink(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    received: list[RootConfig] = []

    def _sink(cfg: RootConfig) -> None:
        received.append(cfg)

    router = Router()
    register_config_methods(router, sink=_sink)
    await router.dispatch({"jsonrpc": "2.0", "id": 1, "method": "config.reload"})
    assert len(received) == 1
