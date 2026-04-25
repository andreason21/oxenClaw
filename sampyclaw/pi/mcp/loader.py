"""Load MCP server config from disk and build an `MCPClientPool`.

Config file: `~/.sampyclaw/mcp.json` by default. Shape::

    {
      "mcpServers": {
        "filesystem": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        },
        "remote": {
          "url": "https://mcp.example.com/sse",
          "headers": {"Authorization": "Bearer ..."}
        }
      }
    }

The `mcpServers` key matches the upstream MCP convention (Claude Desktop,
mcp-cli, etc.) so configs can be shared across clients.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from sampyclaw.config.paths import SampyclawPaths, default_paths
from sampyclaw.pi.mcp.client import MCPClientPool
from sampyclaw.pi.mcp.config import MCPServerConfig, parse_servers_map
from sampyclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("pi.mcp.loader")

_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _expand_env(value: str) -> str:
    """Expand `$VAR` and `${VAR}` references against `os.environ`.

    Unknown vars are left as the literal reference so the user notices.
    """

    def repl(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        if name in os.environ:
            return os.environ[name]
        return match.group(0)

    return _ENV_REF_RE.sub(repl, value)


def _expand_env_in_obj(obj: object) -> object:
    if isinstance(obj, str):
        return _expand_env(obj)
    if isinstance(obj, dict):
        return {k: _expand_env_in_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_in_obj(v) for v in obj]
    return obj


def load_mcp_configs(
    paths: SampyclawPaths | None = None,
    *,
    config_path: Path | None = None,
) -> tuple[list[MCPServerConfig], list[tuple[str, str]]]:
    """Read `mcp.json` and parse it.

    Returns `(configs, diagnostics)`. Missing file = no configs (no error).
    """
    resolved = paths or default_paths()
    target = config_path if config_path is not None else resolved.mcp_config_file
    if not target.exists():
        return [], []
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [], [(str(target), f"json decode failed: {exc}")]
    if not isinstance(raw, dict):
        return [], [(str(target), "top-level value must be an object")]
    servers = raw.get("mcpServers")
    if not isinstance(servers, dict):
        return [], [
            (str(target), "missing or non-object 'mcpServers' map")
        ]
    expanded = _expand_env_in_obj(servers)
    return parse_servers_map(expanded)  # type: ignore[arg-type]


def build_pool_from_config(
    paths: SampyclawPaths | None = None,
    *,
    config_path: Path | None = None,
) -> MCPClientPool | None:
    """Convenience: load configs and return a connected `MCPClientPool`.

    Returns `None` if no servers are configured (so callers can skip wiring
    cheaply). Diagnostics are logged at WARNING.
    """
    configs, diagnostics = load_mcp_configs(paths, config_path=config_path)
    for source, reason in diagnostics:
        logger.warning("mcp config: %s: %s", source, reason)
    if not configs:
        return None
    return MCPClientPool(configs)


__all__ = [
    "build_pool_from_config",
    "load_mcp_configs",
]
