"""mcp.* JSON-RPC methods for managing MCP server configs from the dashboard.

Reads/writes the same `~/.oxenclaw/mcp.json` that `oxenclaw.pi.mcp.loader`
consumes at gateway startup, so dashboard edits round-trip with manual file
edits and with other clients that share the upstream `mcpServers` convention
(Claude Desktop, mcp-cli).

Two transports are exposed:

- **stdio** — child process; requires `command`, optional `args` / `env` / `cwd`
- **http**  — remote SSE / streamable-http; requires `url`, optional
  `headers` / `transport`

All add/update payloads are validated through `parse_server_config` from
`oxenclaw.pi.mcp.config` so the dashboard can never persist an entry the
loader would later reject.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from oxenclaw.config.paths import OxenclawPaths, default_paths
from oxenclaw.gateway.router import Router
from oxenclaw.pi.mcp.client import MCPClient
from oxenclaw.pi.mcp.config import (
    HttpServerConfig,
    StdioServerConfig,
    _ParseFailure,
    parse_server_config,
)

_MCP_SERVERS_KEY = "mcpServers"

# Cap mcp.test wall time so the dashboard never hangs on a misconfigured
# server. The loader respects each server's own connectionTimeoutMs at
# real startup; for the interactive "test" probe we want a tighter ceiling.
_TEST_CONNECT_TIMEOUT_S = 15.0
_TEST_LIST_TIMEOUT_S = 10.0


class _NameParam(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


class _ServerSpec(BaseModel):
    """Dashboard-side server entry. Mirrors mcp.json but with explicit `kind`
    so we don't have to infer transport from which fields happen to be set."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["stdio", "http"] = "stdio"
    # stdio
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    # http
    url: str | None = None
    transport: Literal["sse", "streamable-http"] | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    # shared
    connection_timeout_ms: int | None = None


def _read_mcp_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_mcp_json(path: Path, data: dict[str, Any]) -> None:
    """Atomic replace, mode 0600 — headers can carry bearer tokens."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mcp.", suffix=".json.tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=False)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _serialize_spec(spec: _ServerSpec) -> dict[str, Any]:
    """Translate the typed dashboard spec into the on-disk mcp.json shape."""
    out: dict[str, Any] = {}
    if spec.kind == "stdio":
        if not (spec.command and spec.command.strip()):
            raise ValueError("stdio server requires a non-empty 'command'")
        out["command"] = spec.command
        if spec.args:
            out["args"] = list(spec.args)
        if spec.env:
            out["env"] = dict(spec.env)
        if spec.cwd:
            out["cwd"] = spec.cwd
    else:
        if not (spec.url and spec.url.strip()):
            raise ValueError("http server requires a non-empty 'url'")
        out["url"] = spec.url.strip()
        if spec.transport:
            out["transport"] = spec.transport
        if spec.headers:
            out["headers"] = dict(spec.headers)
    if spec.connection_timeout_ms is not None and spec.connection_timeout_ms > 0:
        out["connectionTimeoutMs"] = int(spec.connection_timeout_ms)
    return out


def _summarize_entry(name: str, raw: Any) -> dict[str, Any]:
    """Build a UI-friendly summary of one mcp.json entry.

    Always returns the raw on-disk dict under `raw` so the dashboard form
    can pre-fill from it (including unexpanded `${VAR}` references), and
    a parsed-side `valid` / `kind` so the UI can show a status badge.
    """
    parsed = (
        parse_server_config(name, raw)
        if isinstance(raw, dict)
        else _ParseFailure("server config must be an object")
    )
    out: dict[str, Any] = {"name": name, "raw": raw if isinstance(raw, dict) else {}}
    if isinstance(parsed, _ParseFailure):
        out.update({"valid": False, "kind": None, "reason": parsed.reason})
        return out
    out["valid"] = True
    out["kind"] = parsed.kind
    out["description"] = parsed.description
    out["connection_timeout_ms"] = int(parsed.connection_timeout_seconds * 1000)
    if isinstance(parsed, StdioServerConfig):
        out["dropped_env_keys"] = list(parsed.dropped_env_keys)
    elif isinstance(parsed, HttpServerConfig):
        out["transport"] = parsed.transport_type
    return out


def register_mcp_methods(
    router: Router,
    *,
    paths: OxenclawPaths | None = None,
) -> None:
    resolved_paths = paths or default_paths()

    def _config_path() -> Path:
        return resolved_paths.mcp_config_file

    @router.method("mcp.list")
    async def _list(_: dict) -> dict[str, Any]:  # type: ignore[type-arg]
        path = _config_path()
        data = _read_mcp_json(path)
        servers_raw = data.get(_MCP_SERVERS_KEY)
        servers = servers_raw if isinstance(servers_raw, dict) else {}
        entries = [_summarize_entry(str(name), raw) for name, raw in servers.items()]
        return {
            "ok": True,
            "config_path": str(path),
            "exists": path.exists(),
            "servers": entries,
        }

    @router.method("mcp.add", _ServerSpec)
    async def _add(p: _ServerSpec) -> dict[str, Any]:
        if not p.name.strip():
            return {"ok": False, "error": "name must not be empty"}
        try:
            raw = _serialize_spec(p)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        parsed = parse_server_config(p.name, raw)
        if isinstance(parsed, _ParseFailure):
            return {"ok": False, "error": parsed.reason}
        path = _config_path()
        data = _read_mcp_json(path)
        servers = data.get(_MCP_SERVERS_KEY)
        if not isinstance(servers, dict):
            servers = {}
            data[_MCP_SERVERS_KEY] = servers
        if p.name in servers:
            return {
                "ok": False,
                "error": f"server '{p.name}' already exists; use mcp.update to change it",
            }
        servers[p.name] = raw
        _write_mcp_json(path, data)
        return {"ok": True, "entry": _summarize_entry(p.name, raw)}

    @router.method("mcp.update", _ServerSpec)
    async def _update(p: _ServerSpec) -> dict[str, Any]:
        if not p.name.strip():
            return {"ok": False, "error": "name must not be empty"}
        try:
            raw = _serialize_spec(p)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        parsed = parse_server_config(p.name, raw)
        if isinstance(parsed, _ParseFailure):
            return {"ok": False, "error": parsed.reason}
        path = _config_path()
        data = _read_mcp_json(path)
        servers = data.get(_MCP_SERVERS_KEY)
        if not isinstance(servers, dict):
            servers = {}
            data[_MCP_SERVERS_KEY] = servers
        servers[p.name] = raw
        _write_mcp_json(path, data)
        return {"ok": True, "entry": _summarize_entry(p.name, raw)}

    @router.method("mcp.delete", _NameParam)
    async def _delete(p: _NameParam) -> dict[str, Any]:
        path = _config_path()
        data = _read_mcp_json(path)
        servers = data.get(_MCP_SERVERS_KEY)
        if not isinstance(servers, dict) or p.name not in servers:
            return {"ok": False, "error": f"server '{p.name}' not found"}
        del servers[p.name]
        _write_mcp_json(path, data)
        return {"ok": True, "name": p.name}

    @router.method("mcp.test", _NameParam)
    async def _test(p: _NameParam) -> dict[str, Any]:
        path = _config_path()
        data = _read_mcp_json(path)
        servers = data.get(_MCP_SERVERS_KEY)
        if not isinstance(servers, dict) or p.name not in servers:
            return {"ok": False, "error": f"server '{p.name}' not found"}
        parsed = parse_server_config(p.name, servers[p.name])
        if isinstance(parsed, _ParseFailure):
            return {"ok": False, "error": parsed.reason}
        client = MCPClient(parsed)
        try:
            connect_timeout = min(parsed.connection_timeout_seconds, _TEST_CONNECT_TIMEOUT_S)
            await asyncio.wait_for(client.connect(), timeout=connect_timeout)
            tools = await asyncio.wait_for(client.list_tools(), timeout=_TEST_LIST_TIMEOUT_S)
        except Exception as exc:
            return {"ok": False, "error": str(exc) or exc.__class__.__name__}
        finally:
            with contextlib.suppress(Exception):
                await client.close()
        return {
            "ok": True,
            "name": p.name,
            "kind": parsed.kind,
            "tools": [
                {
                    "name": t.get("name") if isinstance(t, dict) else None,
                    "description": t.get("description") if isinstance(t, dict) else None,
                }
                for t in tools
            ],
        }


__all__ = ["register_mcp_methods"]
