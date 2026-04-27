"""Tests for runtime skill activation (slash commands)."""

from __future__ import annotations

from pathlib import Path

import pytest

from oxenclaw.clawhub.activation import (
    build_skill_invocation_message,
    detect_skill_slash_command,
)
from oxenclaw.clawhub.frontmatter import SkillManifest
from oxenclaw.clawhub.loader import InstalledSkill


def _make_skill(tmp_path: Path, slug: str, body: str) -> InstalledSkill:
    skill_dir = tmp_path / slug
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {slug}\ndescription: test skill\n---\n{body}",
        encoding="utf-8",
    )
    manifest = SkillManifest.model_validate({"name": slug, "description": "test skill"})
    return InstalledSkill(
        slug=slug,
        manifest=manifest,
        skill_md_path=skill_dir / "SKILL.md",
        body=body,
        origin=None,
    )


def test_detect_slash_command() -> None:
    assert detect_skill_slash_command("/weather seoul") == ("weather", "seoul")
    assert detect_skill_slash_command("/weather") == ("weather", "")
    assert detect_skill_slash_command("hi /weather") is None
    assert detect_skill_slash_command("/") is None
    assert detect_skill_slash_command("") is None
    assert detect_skill_slash_command("/bad command") == ("bad", "command")


def test_build_message_returns_none_for_unknown_slug(tmp_path) -> None:
    skill = _make_skill(tmp_path, "weather", "Look up weather.")
    msg = build_skill_invocation_message("nonexistent", "", skills=[skill])
    assert msg is None


def test_build_message_includes_activation_banner(tmp_path) -> None:
    skill = _make_skill(tmp_path, "weather", "Look up weather.")
    msg = build_skill_invocation_message("weather", "Seoul please", skills=[skill])
    assert msg is not None
    assert "weather" in msg
    assert "IMPORTANT" in msg
    assert "Look up weather." in msg
    assert "Seoul please" in msg
    assert "[Skill directory:" in msg


def test_build_message_lists_supporting_files(tmp_path) -> None:
    skill = _make_skill(tmp_path, "weather", "Body")
    extra = tmp_path / "weather" / "lookup.sh"
    extra.write_text("#!/bin/sh", encoding="utf-8")
    msg = build_skill_invocation_message("weather", "", skills=[skill])
    assert msg is not None
    assert "Supporting files" in msg
    assert "lookup.sh" in msg
