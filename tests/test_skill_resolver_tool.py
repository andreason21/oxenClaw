"""Tests for the skill_resolver pi-runtime tool.

Three test scenarios:
1. Resolves to an already-installed skill matched by description.
2. Returns {"found": "none"} when registries=None and nothing matches.
3. Installs via a fake registry + fake installer, then returns {"found": "installed"}.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from oxenclaw.clawhub.lockfile import OriginMetadata
from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.tools_pkg.skill_resolver_tool import skill_resolver_tool

# ── shared skill SKILL.md content ────────────────────────────────────────────

_SAMPLE_SKILL_MD = """\
---
name: stock-analysis
description: Analyze stock market data and AAPL share price.
metadata:
  openclaw:
    emoji: 📈
---

# Stock Analysis

Use the scripts under scripts/ to pull AAPL data.
"""


def _setup_skill(home: Path, slug: str = "stock-analysis") -> OxenclawPaths:
    """Write a minimal valid skill into tmp_path and return an OxenclawPaths."""
    paths = OxenclawPaths(home=home)
    paths.ensure_home()
    skill_dir = home / "skills" / slug
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_SAMPLE_SKILL_MD)
    (skill_dir / ".clawhub").mkdir()
    OriginMetadata(
        registry="https://clawhub.ai",
        slug=slug,
        installed_version="1.0.0",
        installed_at=1.0,
    ).save(skill_dir / ".clawhub" / "origin.json")
    return paths


def _run_tool_sync(tool, args: dict) -> dict:
    """Synchronously execute a FunctionTool and parse the JSON result."""
    import asyncio

    result_str = asyncio.run(tool.execute(args))
    return json.loads(result_str)


# ── test 1: installed skill matched by description ────────────────────────────


def test_resolver_matches_installed_skill_by_description(tmp_path: Path) -> None:
    """A query whose text appears in the description returns found='installed'."""
    paths = _setup_skill(tmp_path)
    tool = skill_resolver_tool(registries=None, installer=None, paths=paths)

    result = _run_tool_sync(tool, {"query": "AAPL stock", "auto_install": False})

    assert result["found"] == "installed"
    assert result["slug"] == "stock-analysis"
    assert result["name"] == "stock-analysis"
    assert "SKILL.md" in result["skill_md"]
    assert "Stock Analysis" in result["instructions"]


# ── test 2: returns none when no match and no registries ─────────────────────


def test_resolver_returns_none_when_no_match_and_no_registries(tmp_path: Path) -> None:
    """With registries=None and a query that matches nothing, return found='none'."""
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    # No skills written — only bundled ones (which won't match our obscure query).
    tool = skill_resolver_tool(registries=None, installer=None, paths=paths)

    result = _run_tool_sync(
        tool, {"query": "xyzzy-nonexistent-domain-12345", "auto_install": False}
    )

    assert result["found"] == "none"
    assert result["searched"] == "xyzzy-nonexistent-domain-12345"
    assert isinstance(result["registries"], list)


# ── test 3: installs via fake registry + fake installer ───────────────────────


def _make_zip_bytes(slug: str = "stock-analysis") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{slug}/SKILL.md", _SAMPLE_SKILL_MD)
        zf.writestr(f"{slug}/scripts/run.sh", "#!/bin/sh\necho hello\n")
    return buf.getvalue()


def test_resolver_installs_via_fake_registry(tmp_path: Path) -> None:
    """When a remote search returns a hit and auto_install=True, the installer
    is called; after install the tool returns found='installed' and the
    SKILL.md exists on disk."""
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()

    slug = "stock-analysis"

    # Fake registry: search returns one result matching the query.
    fake_client = MagicMock()
    fake_client.search_skills = AsyncMock(
        return_value=[
            {
                "slug": slug,
                "name": slug,
                "description": "Analyze AAPL stock data.",
            }
        ]
    )

    fake_registries = MagicMock()
    fake_registries.get_client.return_value = fake_client
    fake_registries.names.return_value = ["public"]

    # Fake installer: writes SKILL.md into paths.home/skills/<slug>/ on install.
    async def _fake_install(slug_arg: str, **kwargs) -> None:
        target = paths.home / "skills" / slug_arg
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(_SAMPLE_SKILL_MD)
        scripts = target / "scripts"
        scripts.mkdir(exist_ok=True)
        (scripts / "run.sh").write_text("#!/bin/sh\necho hello\n")

    fake_installer = MagicMock()
    fake_installer.install = AsyncMock(side_effect=_fake_install)

    tool = skill_resolver_tool(registries=fake_registries, installer=fake_installer, paths=paths)

    result = _run_tool_sync(tool, {"query": "AAPL stock analysis", "auto_install": True})

    assert result["found"] == "installed", result
    assert result["slug"] == slug
    # SKILL.md must exist on disk.
    skill_md = paths.home / "skills" / slug / "SKILL.md"
    assert skill_md.exists(), f"SKILL.md not found at {skill_md}"
    # scripts_dir path should be reported.
    assert result["scripts_dir"] is not None
    assert Path(result["scripts_dir"]).exists()
