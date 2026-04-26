"""Tests for the MCP client phase (`oxenclaw/pi/mcp/`)."""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from oxenclaw.pi.mcp.adapter import materialize_mcp_tools
from oxenclaw.pi.mcp.client import MCPClient, MCPClientPool, MCPError
from oxenclaw.pi.mcp.config import (
    DANGEROUS_ENV_KEYS,
    HttpServerConfig,
    StdioServerConfig,
    parse_server_config,
    parse_servers_map,
)
from oxenclaw.pi.mcp.names import (
    TOOL_NAME_SEPARATOR,
    build_safe_tool_name,
    sanitize_server_name,
    sanitize_tool_name,
)

# ---------------------------------------------------------------------- config


def test_parse_stdio_minimal():
    cfg = parse_server_config("fs", {"command": "echo", "args": ["hi"]})
    assert isinstance(cfg, StdioServerConfig)
    assert cfg.command == "echo"
    assert cfg.args == ("hi",)
    assert cfg.cwd is None
    assert cfg.kind == "stdio"
    assert "echo" in cfg.description


def test_parse_stdio_strips_dangerous_env():
    cfg = parse_server_config(
        "fs",
        {
            "command": "ls",
            "env": {
                "FOO": "bar",
                "LD_PRELOAD": "/etc/evil.so",
                "PATH": "/usr/bin",
                "SUDO_ASKPASS": "/x",
            },
        },
    )
    assert isinstance(cfg, StdioServerConfig)
    assert cfg.env == {"FOO": "bar"}
    assert set(cfg.dropped_env_keys) == {"LD_PRELOAD", "PATH", "SUDO_ASKPASS"}


def test_parse_http_sse():
    cfg = parse_server_config(
        "remote",
        {
            "url": "https://mcp.example.com/sse",
            "headers": {"Authorization": "Bearer x"},
        },
    )
    assert isinstance(cfg, HttpServerConfig)
    assert cfg.url == "https://mcp.example.com/sse"
    assert cfg.headers == {"Authorization": "Bearer x"}
    assert cfg.transport_type == "sse"
    assert cfg.description == "https://mcp.example.com/sse"


def test_parse_http_streamable():
    cfg = parse_server_config(
        "remote",
        {
            "url": "https://mcp.example.com/rpc",
            "transport": "streamable-http",
        },
    )
    assert isinstance(cfg, HttpServerConfig)
    assert cfg.transport_type == "streamable-http"


def test_parse_rejects_non_http_scheme():
    cfg = parse_server_config("bad", {"url": "ftp://example.com"})
    # _ParseFailure is private — we check by attribute
    assert hasattr(cfg, "reason") and "http" in cfg.reason


def test_parse_rejects_unknown_transport():
    cfg = parse_server_config("bad", {"url": "https://example.com", "transport": "websocket"})
    assert hasattr(cfg, "reason") and "websocket" in cfg.reason


def test_parse_rejects_missing_command_and_url():
    cfg = parse_server_config("bad", {})
    assert hasattr(cfg, "reason")


def test_parse_servers_map_collects_diagnostics():
    configs, diagnostics = parse_servers_map(
        {
            "good": {"command": "echo"},
            "broken": {},
            "ftp": {"url": "ftp://x"},
        }
    )
    names = [c.server_name for c in configs]
    assert names == ["good"]
    diag_names = [d[0] for d in diagnostics]
    assert "broken" in diag_names and "ftp" in diag_names


def test_dangerous_env_keys_match_expectations():
    # Sanity: catch accidental drops if someone edits the constant.
    for key in ("LD_PRELOAD", "PATH", "PYTHONPATH"):
        assert key in DANGEROUS_ENV_KEYS


# ---------------------------------------------------------------------- names


def test_sanitize_server_name_replaces_unsafe_chars():
    used: set[str] = set()
    assert sanitize_server_name("my server!", used) == "my-server-"
    assert "my-server-" in used


def test_sanitize_server_name_dedups():
    used: set[str] = set()
    a = sanitize_server_name("svr", used)
    b = sanitize_server_name("svr", used)
    assert a != b
    assert b.startswith("svr") and b.endswith("-2")


def test_sanitize_tool_name_clamps_to_safe_chars():
    assert sanitize_tool_name("get/foo bar") == "get-foo-bar"
    assert sanitize_tool_name("") == "tool"


def test_build_safe_tool_name_caps_total_length():
    name = build_safe_tool_name(
        server_name="filesystem",
        tool_name="x" * 200,
        reserved_names=set(),
    )
    assert len(name) <= 64
    assert TOOL_NAME_SEPARATOR in name


def test_build_safe_tool_name_dedups_against_reserved():
    reserved = {"fs__read"}
    name = build_safe_tool_name(
        server_name="fs",
        tool_name="read",
        reserved_names=reserved,
    )
    assert name != "fs__read"
    assert name.startswith("fs__read")


# ----------------------------------------------------------- mock transport


class _FakeTransport:
    """In-memory transport used to drive client/initialize tests.

    Exposes `inbox` (server→client) and `outbox` (client→server) so tests
    can script a conversation.
    """

    def __init__(self, server_name: str = "fake") -> None:
        self.server_name = server_name
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.outbox: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, message: dict[str, Any]) -> None:
        self.outbox.append(message)

    async def receive(self) -> dict[str, Any]:
        return await self.inbox.get()

    async def close(self) -> None:
        self.closed = True


def _stdio_cfg() -> StdioServerConfig:
    return StdioServerConfig(server_name="fake", command="noop", args=())


@pytest.mark.asyncio
async def test_client_initialize_and_list_tools_pagination():
    transport = _FakeTransport()
    cfg = _stdio_cfg()
    client = MCPClient(cfg, transport=transport)  # type: ignore[arg-type]

    async def server_script() -> None:
        # initialize response
        msg = await _wait_for_outbox(transport, 1)
        assert msg[0]["method"] == "initialize"
        await transport.inbox.put(
            {
                "jsonrpc": "2.0",
                "id": msg[0]["id"],
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-server", "version": "0.1"},
                },
            }
        )
        # initialized notification arrives next; consume it from the outbox.
        await _wait_for_outbox(transport, 2)

        # tools/list page 1
        msg = await _wait_for_outbox(transport, 3)
        assert msg[2]["method"] == "tools/list"
        await transport.inbox.put(
            {
                "jsonrpc": "2.0",
                "id": msg[2]["id"],
                "result": {
                    "tools": [
                        {
                            "name": "read_file",
                            "description": "Read a file",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"path": {"type": "string"}},
                            },
                        }
                    ],
                    "nextCursor": "page2",
                },
            }
        )
        # tools/list page 2
        msg = await _wait_for_outbox(transport, 4)
        assert msg[3]["method"] == "tools/list"
        assert msg[3]["params"]["cursor"] == "page2"
        await transport.inbox.put(
            {
                "jsonrpc": "2.0",
                "id": msg[3]["id"],
                "result": {
                    "tools": [
                        {
                            "name": "write_file",
                            "description": "Write a file",
                            "inputSchema": {
                                "type": "object",
                            },
                        }
                    ],
                },
            }
        )

    server_task = asyncio.create_task(server_script())
    await client.connect()
    tools = await client.list_tools()
    await client.close()
    await asyncio.wait_for(server_task, timeout=2.0)

    names = [t["name"] for t in tools]
    assert names == ["read_file", "write_file"]
    assert client.is_initialized


async def _wait_for_outbox(
    transport: _FakeTransport, min_len: int, timeout: float = 2.0
) -> list[dict[str, Any]]:
    """Poll `transport.outbox` until its length is ≥ `min_len`."""
    deadline = asyncio.get_running_loop().time() + timeout
    while len(transport.outbox) < min_len:
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"outbox did not reach {min_len} (got {len(transport.outbox)})")
        await asyncio.sleep(0.005)
    return list(transport.outbox)


@pytest.mark.asyncio
async def test_client_call_tool_returns_result():
    transport = _FakeTransport()
    cfg = _stdio_cfg()
    client = MCPClient(cfg, transport=transport)  # type: ignore[arg-type]

    async def server_script() -> None:
        msg = await _wait_for_outbox(transport, 1)
        await transport.inbox.put({"jsonrpc": "2.0", "id": msg[0]["id"], "result": {}})
        await _wait_for_outbox(transport, 2)  # init notification
        msg = await _wait_for_outbox(transport, 3)
        assert msg[2]["method"] == "tools/call"
        assert msg[2]["params"] == {
            "name": "echo",
            "arguments": {"text": "hi"},
        }
        await transport.inbox.put(
            {
                "jsonrpc": "2.0",
                "id": msg[2]["id"],
                "result": {
                    "content": [{"type": "text", "text": "hi"}],
                    "isError": False,
                },
            }
        )

    server_task = asyncio.create_task(server_script())
    await client.connect()
    result = await client.call_tool("echo", {"text": "hi"})
    await client.close()
    await asyncio.wait_for(server_task, timeout=2.0)
    assert result["content"][0]["text"] == "hi"


@pytest.mark.asyncio
async def test_client_call_tool_propagates_jsonrpc_error():
    transport = _FakeTransport()
    cfg = _stdio_cfg()
    client = MCPClient(cfg, transport=transport)  # type: ignore[arg-type]

    async def server_script() -> None:
        msg = await _wait_for_outbox(transport, 1)
        await transport.inbox.put({"jsonrpc": "2.0", "id": msg[0]["id"], "result": {}})
        await _wait_for_outbox(transport, 2)
        msg = await _wait_for_outbox(transport, 3)
        await transport.inbox.put(
            {
                "jsonrpc": "2.0",
                "id": msg[2]["id"],
                "error": {"code": -32601, "message": "no such tool"},
            }
        )

    server_task = asyncio.create_task(server_script())
    await client.connect()
    with pytest.raises(MCPError) as ei:
        await client.call_tool("missing", {})
    assert ei.value.code == -32601
    await client.close()
    await asyncio.wait_for(server_task, timeout=2.0)


# ----------------------------------------------------------- end-to-end stdio


_FAKE_STDIO_SERVER_SCRIPT = textwrap.dedent(
    """
    import json, sys
    def write(msg):
        sys.stdout.write(json.dumps(msg) + "\\n")
        sys.stdout.flush()
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        try:
            req = json.loads(line)
        except Exception:
            continue
        method = req.get("method")
        msg_id = req.get("id")
        if method == "initialize":
            write({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "fake", "version": "0.1"}
                }
            })
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            write({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {"tools": [{
                    "name": "ping",
                    "description": "ping",
                    "inputSchema": {"type": "object", "properties": {}}
                }]}
            })
        elif method == "tools/call":
            args = (req.get("params") or {}).get("arguments") or {}
            text = args.get("text") or "pong"
            write({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": False
                }
            })
        else:
            write({
                "jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": "no such method"}
            })
    """
)


@pytest.mark.asyncio
async def test_stdio_end_to_end_with_real_subprocess(tmp_path: Path):
    script_path = tmp_path / "fake_mcp_server.py"
    script_path.write_text(_FAKE_STDIO_SERVER_SCRIPT)
    cfg = StdioServerConfig(
        server_name="fake-stdio",
        command=sys.executable,
        args=(str(script_path),),
    )
    pool = MCPClientPool([cfg])
    try:
        tools = await materialize_mcp_tools(pool)
        assert len(tools) == 1
        tool = tools[0]
        assert tool.name.startswith("fake-stdio") and "ping" in tool.name
        result = await tool.execute({"text": "hello"})
        assert "hello" in result
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_pool_failure_isolates_one_bad_server(tmp_path: Path):
    """A bad server doesn't crash the pool — its tools simply don't appear."""
    good_script = tmp_path / "good.py"
    good_script.write_text(_FAKE_STDIO_SERVER_SCRIPT)
    bad_cfg = StdioServerConfig(
        server_name="missing",
        command="/nonexistent/binary-please",
        args=(),
        connection_timeout_seconds=2.0,
    )
    good_cfg = StdioServerConfig(
        server_name="good",
        command=sys.executable,
        args=(str(good_script),),
    )
    pool = MCPClientPool([bad_cfg, good_cfg])
    try:
        tools = await materialize_mcp_tools(pool)
        names = [t.name for t in tools]
        assert any(n.startswith("good") for n in names)
        assert not any(n.startswith("missing") for n in names)
        assert "missing" in pool.failures
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_adapter_renames_to_avoid_reserved_collisions(tmp_path: Path):
    script_path = tmp_path / "fake_mcp_server.py"
    script_path.write_text(_FAKE_STDIO_SERVER_SCRIPT)
    cfg = StdioServerConfig(
        server_name="fs",
        command=sys.executable,
        args=(str(script_path),),
    )
    pool = MCPClientPool([cfg])
    try:
        tools = await materialize_mcp_tools(pool, reserved_names=["fs__ping"])
        assert len(tools) == 1
        assert tools[0].name != "fs__ping"
        assert tools[0].name.startswith("fs__ping")
    finally:
        await pool.close()


# ----------------------------------------------------------- loader (mcp.json)


def test_loader_returns_empty_when_file_missing(tmp_path: Path):
    from oxenclaw.config.paths import OxenclawPaths
    from oxenclaw.pi.mcp.loader import load_mcp_configs

    paths = OxenclawPaths(home=tmp_path)
    configs, diagnostics = load_mcp_configs(paths)
    assert configs == [] and diagnostics == []


def test_loader_parses_servers_map(tmp_path: Path):
    from oxenclaw.config.paths import OxenclawPaths
    from oxenclaw.pi.mcp.loader import load_mcp_configs

    paths = OxenclawPaths(home=tmp_path)
    paths.mcp_config_file.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fs": {"command": "echo"},
                    "remote": {"url": "https://mcp.example.com/sse"},
                    "broken": {},
                }
            }
        )
    )
    configs, diagnostics = load_mcp_configs(paths)
    names = sorted(c.server_name for c in configs)
    assert names == ["fs", "remote"]
    assert any(d[0] == "broken" for d in diagnostics)


def test_loader_expands_env_refs(tmp_path: Path, monkeypatch):
    from oxenclaw.config.paths import OxenclawPaths
    from oxenclaw.pi.mcp.loader import load_mcp_configs

    paths = OxenclawPaths(home=tmp_path)
    monkeypatch.setenv("FAKE_TOKEN", "tok-xyz")
    paths.mcp_config_file.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {
                        "url": "https://mcp.example.com/sse",
                        "headers": {"Authorization": "Bearer ${FAKE_TOKEN}"},
                    }
                }
            }
        )
    )
    configs, _ = load_mcp_configs(paths)
    assert len(configs) == 1
    cfg = configs[0]
    assert cfg.headers == {"Authorization": "Bearer tok-xyz"}


def test_loader_reports_bad_json(tmp_path: Path):
    from oxenclaw.config.paths import OxenclawPaths
    from oxenclaw.pi.mcp.loader import load_mcp_configs

    paths = OxenclawPaths(home=tmp_path)
    paths.mcp_config_file.write_text("not json {")
    configs, diagnostics = load_mcp_configs(paths)
    assert configs == []
    assert diagnostics and "json" in diagnostics[0][1].lower()


def test_build_pool_returns_none_when_no_servers(tmp_path: Path):
    from oxenclaw.config.paths import OxenclawPaths
    from oxenclaw.pi.mcp.loader import build_pool_from_config

    paths = OxenclawPaths(home=tmp_path)
    assert build_pool_from_config(paths) is None


# ----------------------------------------------------------- factory wiring


@pytest.mark.asyncio
async def test_factory_load_mcp_tools_integrates_with_build_agent(
    tmp_path: Path,
):
    from oxenclaw.agents.factory import build_agent, load_mcp_tools
    from oxenclaw.config.paths import OxenclawPaths

    script_path = tmp_path / "fake_mcp_server.py"
    script_path.write_text(_FAKE_STDIO_SERVER_SCRIPT)
    paths = OxenclawPaths(home=tmp_path)
    paths.mcp_config_file.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fakefs": {
                        "command": sys.executable,
                        "args": [str(script_path)],
                    }
                }
            }
        )
    )
    tools, pool = await load_mcp_tools(paths)
    try:
        assert pool is not None
        assert len(tools) == 1

        agent = build_agent(
            agent_id="test",
            provider="pi",
            mcp_tools=tools,
        )
        # PiAgent owns a ToolRegistry — confirm the MCP tool was registered
        # alongside the defaults.
        registry_names = agent._tools.names()  # type: ignore[attr-defined]
        assert any(n.startswith("fakefs") for n in registry_names)
        assert "echo" in registry_names  # default tool still present
    finally:
        if pool is not None:
            await pool.close()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_adapter_returns_unavailable_string_when_server_dropped(
    tmp_path: Path,
):
    bad_cfg = StdioServerConfig(
        server_name="dead",
        command="/nope/x",
        args=(),
        connection_timeout_seconds=1.0,
    )
    pool = MCPClientPool([bad_cfg])
    try:
        tools = await materialize_mcp_tools(pool)
        assert tools == []
        # Manually construct a proxy tool that points at the failed server
        # to verify the runtime fallback.
        from oxenclaw.pi.mcp.adapter import _MCPProxyTool

        proxy = _MCPProxyTool(
            name="dead__t",
            description="x",
            input_schema={"type": "object"},
            pool=pool,
            server_name="dead",
            original_tool_name="t",
            timeout_seconds=1.0,
        )
        out = await proxy.execute({})
        assert "unavailable" in out and "dead" in out
    finally:
        await pool.close()
