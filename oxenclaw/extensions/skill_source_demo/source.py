"""Reference `SkillSourcePlugin` — in-memory, deterministic, no network.

Holds a tiny built-in catalog (one demo skill) so the plumbing in
`MultiRegistryClient` → `SkillInstaller` → archive extraction is
exercisable without external dependencies. Real-world plugins
substitute git/HTTP/database calls for the in-memory dicts here.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

from oxenclaw.clawhub.client import sha256_integrity

# A single demonstration skill. The shape of every dict here matches
# what ClawHubClient returns from the real public registry — the
# installer doesn't care whether we built it ourselves or fetched it
# over HTTPS.
_DEMO_SKILL_MD = """---
name: demo-skill
description: Reference skill for the in-memory plugin source demo.
metadata:
  openclaw:
    emoji: 🧪
---

# demo-skill

This skill ships from `oxenclaw.extensions.skill_source_demo` to prove
the plugin path is wired end-to-end. It does nothing useful at
runtime; copy the module pattern when authoring your own skill source.
"""

_DEMO_CATALOG: dict[str, dict[str, Any]] = {
    "demo-skill": {
        "slug": "demo-skill",
        "displayName": "Demo Skill",
        "summary": "Reference skill emitted by the in-memory plugin source.",
        "version": "1.0.0",
        "trust": "community",
    },
}


def _build_zip(slug: str) -> bytes:
    """ZIP whose root contains `<slug>/SKILL.md` — the layout
    `_extract_zip_to` expects."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{slug}/SKILL.md", _DEMO_SKILL_MD)
    return buf.getvalue()


class DemoSkillSource:
    """In-memory skill source. Constructor accepts an `options` dict
    to match the SkillSourcePlugin loader contract; the demo ignores
    every field but `extra_skills` (test seam — lets unit tests inject
    additional fake catalog entries without touching module state).
    """

    def __init__(self, *, options: dict[str, Any] | None = None) -> None:
        opts = options or {}
        extra = opts.get("extra_skills") or {}
        self._catalog: dict[str, dict[str, Any]] = {**_DEMO_CATALOG, **extra}

    async def search_skills(
        self, query: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        q = (query or "").lower().strip()
        hits = [
            entry
            for entry in self._catalog.values()
            if not q
            or q in entry["slug"].lower()
            or q in (entry.get("summary") or "").lower()
        ]
        if limit:
            hits = hits[:limit]
        return hits

    async def list_skills(
        self, *, limit: int | None = None
    ) -> dict[str, Any]:
        results = list(self._catalog.values())
        if limit:
            results = results[:limit]
        return {"results": results, "filtered_count": 0}

    async def fetch_skill_detail(self, slug: str) -> dict[str, Any]:
        if slug not in self._catalog:
            raise KeyError(f"demo source has no skill {slug!r}")
        entry = self._catalog[slug]
        return {
            "skill": entry,
            "latestVersion": {"version": entry.get("version", "1.0.0")},
        }

    async def download_skill_archive(
        self, slug: str, *, version: str | None = None
    ) -> tuple[bytes, str]:
        if slug not in self._catalog:
            raise KeyError(f"demo source has no skill {slug!r}")
        archive = _build_zip(slug)
        return archive, sha256_integrity(archive)

    async def aclose(self) -> None:
        # No held resources — protocol still requires the method.
        return None
