"""MCP server config parsing.

Mirrors openclaw `src/agents/mcp-config-shared.ts` + `mcp-stdio.ts` +
`mcp-http.ts` + `mcp-transport-config.ts`.

Two transport variants are recognised:

- **stdio** — the server is a child process. Config has `command` and
  optional `args`, `env`, `cwd`. Dangerous host env keys (`LD_PRELOAD`,
  `PATH`, `SUDO_*`, etc.) are stripped before launch.
- **http** — the server is reachable over HTTP/SSE. Config has `url` and
  optional `headers`. Only `http`/`https` schemes are accepted.

The parser returns `None` for invalid config (with a `reason`) so a single
malformed server entry doesn't take down the rest of a config map.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

# Env keys that must NEVER be propagated into a child stdio MCP server —
# they affect process launch / loader behavior and are a privilege-escalation
# vector when copied from a config file. Mirror of openclaw's
# `HOST_DANGEROUS_ENV_KEYS` minus the platform-specific WIN entries.
DANGEROUS_ENV_KEYS: frozenset[str] = frozenset(
    {
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "DYLD_FALLBACK_LIBRARY_PATH",
        "PATH",
        "SHELL",
        "IFS",
        "PS1",
        "PS2",
        "PROMPT_COMMAND",
        "BASH_ENV",
        "ENV",
        "PYTHONSTARTUP",
        "PYTHONPATH",
        "NODE_OPTIONS",
        "RUBYOPT",
        "PERL5LIB",
        "PERL5OPT",
    }
)
DANGEROUS_ENV_PREFIXES: tuple[str, ...] = (
    "SUDO_",
    "BASH_FUNC_",
)

DEFAULT_CONNECTION_TIMEOUT_SECONDS: float = 30.0


def _is_dangerous_env_key(key: str) -> bool:
    if key in DANGEROUS_ENV_KEYS:
        return True
    return any(key.startswith(prefix) for prefix in DANGEROUS_ENV_PREFIXES)


def _coerce_string_record(
    value: Any, *, drop_dangerous_keys: bool = False
) -> tuple[dict[str, str], list[str]]:
    """Coerce a dict-like into `dict[str, str]`, dropping non-scalar values.

    Returns `(record, dropped_keys)` so callers can warn about silently
    discarded entries.
    """
    if not isinstance(value, dict):
        return {}, []
    out: dict[str, str] = {}
    dropped: list[str] = []
    for key, entry in value.items():
        if not isinstance(key, str):
            continue
        if drop_dangerous_keys and _is_dangerous_env_key(key):
            dropped.append(key)
            continue
        if isinstance(entry, str):
            out[key] = entry
        elif isinstance(entry, (int, float, bool)):
            out[key] = str(entry)
        else:
            dropped.append(key)
    return out, dropped


def _coerce_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


@dataclass(frozen=True)
class StdioServerConfig:
    """Stdio-transport MCP server (a subprocess)."""

    server_name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] | None = None
    cwd: str | None = None
    connection_timeout_seconds: float = DEFAULT_CONNECTION_TIMEOUT_SECONDS
    dropped_env_keys: tuple[str, ...] = ()

    @property
    def kind(self) -> Literal["stdio"]:
        return "stdio"

    @property
    def description(self) -> str:
        suffix = " " + " ".join(self.args) if self.args else ""
        cwd = f" (cwd={self.cwd})" if self.cwd else ""
        return f"{self.command}{suffix}{cwd}"


@dataclass(frozen=True)
class HttpServerConfig:
    """HTTP-transport MCP server (SSE or streamable-http)."""

    server_name: str
    url: str
    headers: dict[str, str] | None = None
    transport_type: Literal["sse", "streamable-http"] = "sse"
    connection_timeout_seconds: float = DEFAULT_CONNECTION_TIMEOUT_SECONDS

    @property
    def kind(self) -> Literal["http"]:
        return "http"

    @property
    def description(self) -> str:
        # Redact userinfo / query if present.
        try:
            parsed = urlparse(self.url)
            netloc = parsed.hostname or ""
            if parsed.port is not None:
                netloc += f":{parsed.port}"
            redacted = f"{parsed.scheme}://{netloc}{parsed.path or ''}"
            return redacted or self.url
        except Exception:
            return self.url


MCPServerConfig = StdioServerConfig | HttpServerConfig


@dataclass(frozen=True)
class _ParseFailure:
    reason: str


def _connection_timeout(raw: dict[str, Any]) -> float:
    raw_value = raw.get("connectionTimeoutMs", raw.get("connection_timeout_ms"))
    if isinstance(raw_value, (int, float)) and raw_value > 0:
        return float(raw_value) / 1000.0
    return DEFAULT_CONNECTION_TIMEOUT_SECONDS


def parse_server_config(server_name: str, raw: Any) -> MCPServerConfig | _ParseFailure:
    """Parse one server entry into a transport config or a parse failure.

    The shape mirrors openclaw `mcp.json`::

        {
          "stdio-example": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            "env": {"FOO": "bar"}
          },
          "http-example": {
            "url": "https://mcp.example.com/sse",
            "transport": "sse",
            "headers": {"Authorization": "Bearer xxx"}
          }
        }
    """
    if not isinstance(raw, dict):
        return _ParseFailure("server config must be an object")
    requested_transport = raw.get("transport")
    if isinstance(requested_transport, str):
        requested_transport = requested_transport.strip().lower()
    else:
        requested_transport = ""

    command = raw.get("command")
    if isinstance(command, str) and command.strip():
        env_raw = raw.get("env")
        env, dropped = _coerce_string_record(env_raw, drop_dangerous_keys=True)
        cwd_raw = raw.get("cwd") or raw.get("workingDirectory")
        cwd = cwd_raw if isinstance(cwd_raw, str) and cwd_raw.strip() else None
        return StdioServerConfig(
            server_name=server_name,
            command=command,
            args=tuple(_coerce_string_list(raw.get("args"))),
            env=env if env else None,
            cwd=cwd,
            connection_timeout_seconds=_connection_timeout(raw),
            dropped_env_keys=tuple(dropped),
        )

    url = raw.get("url")
    if not (isinstance(url, str) and url.strip()):
        return _ParseFailure("neither 'command' (stdio) nor 'url' (http) was provided")
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return _ParseFailure(
            f"only http/https URLs are supported, got '{parsed.scheme or '<empty>'}'"
        )

    headers, _dropped_headers = _coerce_string_record(raw.get("headers"), drop_dangerous_keys=False)

    if requested_transport and requested_transport not in {
        "sse",
        "streamable-http",
    }:
        return _ParseFailure(
            f"transport '{requested_transport}' is not supported (use 'sse' or 'streamable-http')"
        )

    transport_type: Literal["sse", "streamable-http"] = (
        "streamable-http" if requested_transport == "streamable-http" else "sse"
    )

    return HttpServerConfig(
        server_name=server_name,
        url=url.strip(),
        headers=headers if headers else None,
        transport_type=transport_type,
        connection_timeout_seconds=_connection_timeout(raw),
    )


def parse_servers_map(
    servers: dict[str, Any],
) -> tuple[list[MCPServerConfig], list[tuple[str, str]]]:
    """Parse a top-level `mcpServers` map.

    Returns `(configs, diagnostics)` where each diagnostic is
    `(server_name, reason)` for entries that failed to parse.
    """
    configs: list[MCPServerConfig] = []
    diagnostics: list[tuple[str, str]] = []
    for name, raw in servers.items():
        if not isinstance(name, str) or not name.strip():
            diagnostics.append(("<unnamed>", "server key must be a non-empty string"))
            continue
        result = parse_server_config(name, raw)
        if isinstance(result, _ParseFailure):
            diagnostics.append((name, result.reason))
            continue
        configs.append(result)
    return configs, diagnostics
