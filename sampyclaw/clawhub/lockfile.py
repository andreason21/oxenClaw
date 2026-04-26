"""Persistent install state: lockfile + per-skill origin metadata.

Layout:
    ~/.sampyclaw/.clawhub/lock.json           ← Lockfile (registry-wide)
    ~/.sampyclaw/skills/<slug>/.clawhub/origin.json ← per-skill origin

Mirrors openclaw `src/agents/skills-clawhub.ts` lockfile shape. Atomic
write via tmpfile + rename.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class LockEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    installed_at: float


class Lockfile(BaseModel):
    """Tracks every installed skill version. Persists to disk."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    skills: dict[str, LockEntry] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Lockfile:
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return cls()
        try:
            return cls.model_validate(data)
        except Exception:
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self.model_dump_json(indent=2, exclude_none=False), encoding="utf-8")
        os.replace(tmp, path)

    def upsert(self, slug: str, version: str, *, installed_at: float | None = None) -> None:
        self.skills[slug] = LockEntry(
            version=version,
            installed_at=installed_at if installed_at is not None else time.time(),
        )

    def remove(self, slug: str) -> bool:
        return self.skills.pop(slug, None) is not None


class OriginMetadata(BaseModel):
    """Per-skill `<dir>/.clawhub/origin.json` — provenance for one install."""

    model_config = ConfigDict(extra="allow")

    version: int = 1
    registry: str  # base URL the archive was downloaded from
    registry_name: str | None = None  # logical name from clawhub.registries
    trust: str | None = None  # 'official' | 'mirror' | 'community'
    slug: str
    installed_version: str
    installed_at: float
    integrity: str | None = None  # e.g. "sha256-deadbeef…"

    @classmethod
    def load(cls, path: Path) -> OriginMetadata | None:
        if not path.exists():
            return None
        try:
            return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
        os.replace(tmp, path)
