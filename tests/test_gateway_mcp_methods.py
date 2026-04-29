"""Tests for mcp.* JSON-RPC methods (CRUD over ~/.oxenclaw/mcp.json)."""

from __future__ import annotations

import json
from pathlib import Path

from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.gateway.mcp_methods import register_mcp_methods
from oxenclaw.gateway.router import Router


def _build_router(tmp_path: Path) -> Router:
    router = Router()
    register_mcp_methods(router, paths=OxenclawPaths(home=tmp_path))
    return router


async def _call(router: Router, method: str, params: dict | None = None) -> dict:
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    )
    assert resp.error is None, f"{method} errored: {resp.error}"
    return resp.result


async def test_list_empty_when_no_file(tmp_path: Path) -> None:
    router = _build_router(tmp_path)
    r = await _call(router, "mcp.list")
    assert r["ok"] is True
    assert r["servers"] == []
    assert r["exists"] is False
    assert r["config_path"].endswith("mcp.json")


async def test_add_stdio_server_round_trips_to_disk(tmp_path: Path) -> None:
    router = _build_router(tmp_path)
    r = await _call(
        router,
        "mcp.add",
        {
            "name": "filesystem",
            "kind": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            "env": {"FOO": "bar"},
        },
    )
    assert r["ok"] is True
    assert r["entry"]["valid"] is True
    assert r["entry"]["kind"] == "stdio"

    on_disk = json.loads((tmp_path / "mcp.json").read_text())
    assert on_disk["mcpServers"]["filesystem"]["command"] == "npx"
    assert on_disk["mcpServers"]["filesystem"]["env"] == {"FOO": "bar"}

    listed = await _call(router, "mcp.list")
    assert [s["name"] for s in listed["servers"]] == ["filesystem"]
    assert listed["servers"][0]["kind"] == "stdio"
    assert listed["exists"] is True


async def test_add_http_server_validates_scheme(tmp_path: Path) -> None:
    router = _build_router(tmp_path)
    bad = await _call(
        router,
        "mcp.add",
        {"name": "weird", "kind": "http", "url": "ftp://example.com/foo"},
    )
    assert bad["ok"] is False
    assert "http" in bad["error"]

    good = await _call(
        router,
        "mcp.add",
        {
            "name": "remote",
            "kind": "http",
            "url": "https://mcp.example.com/sse",
            "transport": "sse",
            "headers": {"Authorization": "Bearer xxx"},
        },
    )
    assert good["ok"] is True
    assert good["entry"]["kind"] == "http"
    assert good["entry"]["transport"] == "sse"


async def test_add_stdio_without_command_rejected(tmp_path: Path) -> None:
    router = _build_router(tmp_path)
    r = await _call(router, "mcp.add", {"name": "nope", "kind": "stdio"})
    assert r["ok"] is False
    assert "command" in r["error"]


async def test_add_duplicate_rejected(tmp_path: Path) -> None:
    router = _build_router(tmp_path)
    payload = {"name": "fs", "kind": "stdio", "command": "echo"}
    first = await _call(router, "mcp.add", payload)
    assert first["ok"] is True
    second = await _call(router, "mcp.add", payload)
    assert second["ok"] is False
    assert "already exists" in second["error"]


async def test_update_overwrites_existing_entry(tmp_path: Path) -> None:
    router = _build_router(tmp_path)
    await _call(router, "mcp.add", {"name": "fs", "kind": "stdio", "command": "echo"})
    r = await _call(
        router,
        "mcp.update",
        {"name": "fs", "kind": "stdio", "command": "true", "args": ["--quiet"]},
    )
    assert r["ok"] is True
    on_disk = json.loads((tmp_path / "mcp.json").read_text())
    assert on_disk["mcpServers"]["fs"]["command"] == "true"
    assert on_disk["mcpServers"]["fs"]["args"] == ["--quiet"]
    # Old `env` field should be gone — update is a replace, not a merge.
    assert "env" not in on_disk["mcpServers"]["fs"]


async def test_delete_removes_entry(tmp_path: Path) -> None:
    router = _build_router(tmp_path)
    await _call(router, "mcp.add", {"name": "fs", "kind": "stdio", "command": "echo"})
    r = await _call(router, "mcp.delete", {"name": "fs"})
    assert r["ok"] is True
    listed = await _call(router, "mcp.list")
    assert listed["servers"] == []


async def test_delete_unknown_server_returns_error(tmp_path: Path) -> None:
    router = _build_router(tmp_path)
    r = await _call(router, "mcp.delete", {"name": "ghost"})
    assert r["ok"] is False
    assert "not found" in r["error"]


async def test_dangerous_env_keys_are_stripped(tmp_path: Path) -> None:
    """LD_PRELOAD / PATH etc. must not survive into a stdio launch.

    The parser strips them with `dropped_env_keys` reported back to the UI.
    """
    router = _build_router(tmp_path)
    r = await _call(
        router,
        "mcp.add",
        {
            "name": "danger",
            "kind": "stdio",
            "command": "echo",
            "env": {"SAFE": "ok", "LD_PRELOAD": "/evil.so", "PATH": "/usr/bin"},
        },
    )
    assert r["ok"] is True
    dropped = set(r["entry"]["dropped_env_keys"])
    assert {"LD_PRELOAD", "PATH"} <= dropped
    assert "SAFE" not in dropped


async def test_list_surfaces_invalid_entries(tmp_path: Path) -> None:
    """Hand-edited mcp.json with a malformed entry should be visible (not silently
    dropped) so the user can fix it through the UI."""
    (tmp_path / "mcp.json").write_text(
        json.dumps({"mcpServers": {"broken": {"transport": "sse"}}}),  # no url, no command
        encoding="utf-8",
    )
    router = _build_router(tmp_path)
    r = await _call(router, "mcp.list")
    assert len(r["servers"]) == 1
    entry = r["servers"][0]
    assert entry["name"] == "broken"
    assert entry["valid"] is False
    assert entry["reason"]


async def test_mcp_json_written_with_user_only_perms(tmp_path: Path) -> None:
    """Headers can carry bearer tokens — the file must be 0600."""
    router = _build_router(tmp_path)
    await _call(
        router,
        "mcp.add",
        {
            "name": "remote",
            "kind": "http",
            "url": "https://mcp.example.com/sse",
            "headers": {"Authorization": "Bearer secret"},
        },
    )
    mode = (tmp_path / "mcp.json").stat().st_mode & 0o777
    assert mode == 0o600
