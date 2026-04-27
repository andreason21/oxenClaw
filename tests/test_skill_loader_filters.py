"""Tests for loader filters (platforms / disabled / external_dirs)."""

from __future__ import annotations

import json
import platform as host_platform_mod
from pathlib import Path

import pytest

from oxenclaw.clawhub.loader import (
    _host_platform,
    _platforms_from_manifest,
    load_installed_skills,
)
from oxenclaw.clawhub.frontmatter import SkillManifest
from oxenclaw.config.paths import OxenclawPaths


def _write_skill(root: Path, slug: str, *, platforms: list[str] | None = None) -> Path:
    skill_dir = root / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {slug}\ndescription: test\n"
    if platforms:
        fm += f"platforms: [{', '.join(platforms)}]\n"
    fm += "---\nbody\n"
    (skill_dir / "SKILL.md").write_text(fm, encoding="utf-8")
    return skill_dir


@pytest.fixture
def fake_paths(tmp_path) -> OxenclawPaths:
    home = tmp_path / "oxenclaw_home"
    home.mkdir()
    (home / "skills").mkdir()
    return OxenclawPaths(home=home)


def test_host_platform_normalises() -> None:
    assert _host_platform() in {"linux", "macos", "windows", host_platform_mod.system().lower()}


def test_disabled_slug_filtered(fake_paths) -> None:
    user_root = fake_paths.home / "skills"
    _write_skill(user_root, "weather")
    _write_skill(user_root, "blocker")
    cfg = {"disabled": ["blocker"]}
    (fake_paths.home / "skills_config.json").write_text(json.dumps(cfg))
    skills = load_installed_skills(fake_paths, include_bundled=False)
    slugs = {s.slug for s in skills}
    assert "weather" in slugs
    assert "blocker" not in slugs


def test_platform_disabled_filtered(fake_paths, monkeypatch) -> None:
    user_root = fake_paths.home / "skills"
    _write_skill(user_root, "macos-only")
    cfg = {"platform_disabled": {"macos-only": ["linux", "macos", "windows"]}}
    (fake_paths.home / "skills_config.json").write_text(json.dumps(cfg))
    skills = load_installed_skills(fake_paths, include_bundled=False)
    assert all(s.slug != "macos-only" for s in skills)


def test_platforms_frontmatter_filters(fake_paths, monkeypatch) -> None:
    user_root = fake_paths.home / "skills"
    # Pin host platform to linux so the test is deterministic.
    monkeypatch.setattr("oxenclaw.clawhub.loader._host_platform", lambda: "linux")
    _write_skill(user_root, "linux-only", platforms=["linux"])
    _write_skill(user_root, "mac-only", platforms=["macos"])
    skills = load_installed_skills(fake_paths, include_bundled=False)
    slugs = {s.slug for s in skills}
    assert "linux-only" in slugs
    assert "mac-only" not in slugs


def test_external_dirs_appended(fake_paths, tmp_path) -> None:
    extra_root = tmp_path / "extra"
    extra_root.mkdir()
    _write_skill(extra_root, "from-extra")
    cfg = {"external_dirs": [str(extra_root)]}
    (fake_paths.home / "skills_config.json").write_text(json.dumps(cfg))
    skills = load_installed_skills(fake_paths, include_bundled=False)
    slugs = {s.slug for s in skills}
    assert "from-extra" in slugs


def test_user_overrides_external_overrides_bundled(fake_paths, tmp_path, monkeypatch) -> None:
    user_root = fake_paths.home / "skills"
    extra_root = tmp_path / "extra"
    extra_root.mkdir()
    _write_skill(user_root, "shared")
    _write_skill(extra_root, "shared")
    cfg = {"external_dirs": [str(extra_root)]}
    (fake_paths.home / "skills_config.json").write_text(json.dumps(cfg))
    skills = load_installed_skills(fake_paths, include_bundled=False)
    # exactly one entry for `shared` — user wins.
    matches = [s for s in skills if s.slug == "shared"]
    assert len(matches) == 1
    assert str(matches[0].skill_md_path).startswith(str(user_root))


def test_platforms_from_manifest_reads_top_level() -> None:
    manifest = SkillManifest.model_validate(
        {"name": "x", "description": "y", "platforms": ["linux", "macos"]}
    )
    assert _platforms_from_manifest(manifest) == ["linux", "macos"]
