"""Filesystem layout for oxenclaw config, credentials, and state.

Mirrors openclaw's `~/.openclaw/` layout but rooted at `~/.oxenclaw/` by default.
Override with `OXENCLAW_HOME=<dir>`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OxenclawPaths:
    """Resolved filesystem paths for a oxenclaw installation."""

    home: Path

    @property
    def config_file(self) -> Path:
        return self.home / "config.yaml"

    @property
    def credentials_dir(self) -> Path:
        return self.home / "credentials"

    @property
    def agents_dir(self) -> Path:
        return self.home / "agents"

    @property
    def plugins_dir(self) -> Path:
        return self.home / "plugins"

    @property
    def mcp_config_file(self) -> Path:
        """Path to the user's MCP server map (`~/.oxenclaw/mcp.json`)."""
        return self.home / "mcp.json"

    def credential_file(self, channel: str, account_id: str) -> Path:
        return self.credentials_dir / channel / f"{account_id}.json"

    def agent_dir(self, agent_id: str) -> Path:
        return self.agents_dir / agent_id

    def session_file(self, agent_id: str, session_key: str) -> Path:
        return self.agent_dir(agent_id) / "sessions" / f"{session_key}.json"

    def ensure_home(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.credentials_dir.mkdir(parents=True, exist_ok=True)
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)


def default_paths() -> OxenclawPaths:
    """Resolve paths from env or user home."""
    override = os.environ.get("OXENCLAW_HOME")
    if override:
        return OxenclawPaths(home=Path(override).expanduser().resolve())
    return OxenclawPaths(home=Path.home() / ".oxenclaw")
