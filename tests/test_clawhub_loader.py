"""Skill loader + agent-prompt formatter tests."""

from __future__ import annotations

from oxenclaw.clawhub.loader import (
    format_skills_for_prompt,
    load_installed_skills,
)
from oxenclaw.clawhub.lockfile import OriginMetadata
from oxenclaw.config.paths import OxenclawPaths

SAMPLE_SKILL = """---
name: hello
description: Say hello.
metadata:
  openclaw:
    emoji: 👋
---

# body
"""


def _setup_skill(home, slug: str = "hello") -> OxenclawPaths:  # type: ignore[no-untyped-def]
    paths = OxenclawPaths(home=home)
    paths.ensure_home()
    skill_dir = home / "skills" / slug
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SAMPLE_SKILL)
    (skill_dir / ".clawhub").mkdir()
    OriginMetadata(
        registry="https://clawhub.ai",
        slug=slug,
        installed_version="1.0.0",
        installed_at=1.0,
    ).save(skill_dir / ".clawhub" / "origin.json")
    return paths


def test_load_installed_skills_returns_empty_when_no_dir(tmp_path) -> None:  # type: ignore[no-untyped-def]
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    assert load_installed_skills(paths, include_bundled=False) == []


def test_load_skips_dirs_without_skill_md(tmp_path) -> None:  # type: ignore[no-untyped-def]
    paths = _setup_skill(tmp_path)
    (tmp_path / "skills" / "empty").mkdir()
    out = load_installed_skills(paths, include_bundled=False)
    assert {s.slug for s in out} == {"hello"}


def test_load_skips_malformed_skill_md(tmp_path) -> None:  # type: ignore[no-untyped-def]
    paths = _setup_skill(tmp_path)
    bad = tmp_path / "skills" / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("not yaml frontmatter")
    out = load_installed_skills(paths, include_bundled=False)
    assert {s.slug for s in out} == {"hello"}


def test_loaded_skill_carries_origin(tmp_path) -> None:  # type: ignore[no-untyped-def]
    paths = _setup_skill(tmp_path)
    out = load_installed_skills(paths, include_bundled=False)
    assert len(out) == 1
    s = out[0]
    assert s.manifest.name == "hello"
    assert s.origin is not None
    assert s.origin.installed_version == "1.0.0"


def test_load_includes_bundled_skills_by_default(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Out-of-the-box, the loader returns all 6 curated bundled skills
    so the model can call them without any user-side install."""
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    out = load_installed_skills(paths)
    slugs = {s.slug for s in out}
    # The exact bundled set: weather, github, summarize, healthcheck,
    # session_logs, skill_creator, coding_agent.
    assert "weather" in slugs
    assert "github" in slugs
    assert "skill_creator" in slugs


def test_user_skills_override_bundled_with_same_slug(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """If the user writes their own ~/.oxenclaw/skills/weather/SKILL.md,
    that wins over the bundled weather."""
    paths = _setup_skill(tmp_path, slug="weather")
    out = load_installed_skills(paths)
    weather = next(s for s in out if s.slug == "weather")
    # User-set frontmatter has name="hello" — bundled weather has name="weather".
    assert weather.manifest.name == "hello"


def test_format_skills_block_shape(tmp_path) -> None:  # type: ignore[no-untyped-def]
    paths = _setup_skill(tmp_path)
    skills = load_installed_skills(paths)
    block = format_skills_for_prompt(skills)
    assert "<available_skills>" in block
    assert "<name>hello</name>" in block
    assert "<description>Say hello.</description>" in block
    assert "<location>" in block


def test_format_skills_empty_returns_empty_string() -> None:
    assert format_skills_for_prompt([]) == ""


def test_xml_escape_in_description(tmp_path) -> None:  # type: ignore[no-untyped-def]
    paths = _setup_skill(tmp_path)
    bad = tmp_path / "skills" / "esc"
    bad.mkdir()
    (bad / "SKILL.md").write_text("---\nname: esc\ndescription: a < b & c > d\n---\nbody\n")
    skills = load_installed_skills(paths)
    block = format_skills_for_prompt(skills)
    assert "&lt; b &amp; c &gt;" in block
