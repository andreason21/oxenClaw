"""Plugin manifest — the declarative descriptor every channel plugin ships.

Port of openclaw `packages/plugin-package-contract` / `openclaw.plugin.json`.
Must be loadable WITHOUT importing plugin runtime code (manifest-first
discovery), so the core can enumerate/enable/disable plugins before
accepting their bytes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Manifest(BaseModel):
    """Shape of `manifest.json` (a.k.a. `sampyclaw.plugin.json`)."""

    model_config = ConfigDict(extra="allow")

    id: str
    name: str | None = None
    channels: list[str] = Field(default_factory=list)
    channel_env_vars: dict[str, list[str]] = Field(
        default_factory=dict, alias="channelEnvVars"
    )
    config_schema: dict[str, Any] = Field(
        default_factory=dict, alias="configSchema"
    )

    @classmethod
    def from_path(cls, path: str | Path) -> Manifest:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(raw)

    @classmethod
    def from_json(cls, text: str) -> Manifest:
        return cls.model_validate(json.loads(text))
