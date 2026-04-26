"""SKILL.md frontmatter parser tests."""

from __future__ import annotations

import pytest

from sampyclaw.clawhub.frontmatter import (
    SkillManifestError,
    is_valid_slug,
    parse_skill_text,
)

SAMPLE = """---
name: gifgrep
description: Search GIF providers, download stills, etc.
homepage: https://gifgrep.com
metadata:
  openclaw:
    emoji: 🧲
    primaryEnv: GIFGREP_TOKEN
    requires:
      bins: [gifgrep]
      anyBins: [ffmpeg, ffprobe]
    install:
      - id: brew
        kind: brew
        formula: example/tap/gifgrep
        bins: [gifgrep]
        os: [darwin]
        stripComponents: 1
        targetDir: ./bin
---

# Body

This is the agent-facing markdown body.
"""


def test_parse_full_manifest_round_trip() -> None:
    m, body = parse_skill_text(SAMPLE)
    assert m.name == "gifgrep"
    assert m.homepage == "https://gifgrep.com"
    assert m.openclaw.emoji == "🧲"
    assert m.openclaw.primary_env == "GIFGREP_TOKEN"
    assert m.openclaw.requires.bins == ["gifgrep"]
    assert m.openclaw.requires.any_bins == ["ffmpeg", "ffprobe"]
    spec = m.openclaw.install[0]
    assert spec.kind == "brew"
    assert spec.formula == "example/tap/gifgrep"
    assert spec.os == ["darwin"]
    assert spec.strip_components == 1
    assert spec.target_dir == "./bin"
    assert "Body" in body


def test_minimal_manifest() -> None:
    m, body = parse_skill_text("---\nname: foo\ndescription: bar\n---\nbody here\n")
    assert m.name == "foo"
    assert m.openclaw.requires.bins == []
    assert body == "body here\n"


def test_missing_opening_delimiter() -> None:
    with pytest.raises(SkillManifestError):
        parse_skill_text("no frontmatter here\n")


def test_missing_closing_delimiter() -> None:
    with pytest.raises(SkillManifestError):
        parse_skill_text("---\nname: foo\ndescription: bar\nno closing\n")


def test_malformed_yaml() -> None:
    with pytest.raises(SkillManifestError):
        parse_skill_text("---\nname: [unclosed\n---\n")


def test_invalid_slug_name_rejected() -> None:
    with pytest.raises(SkillManifestError):
        parse_skill_text("---\nname: not a slug!\ndescription: x\n---\n")


def test_root_must_be_mapping() -> None:
    with pytest.raises(SkillManifestError):
        parse_skill_text("---\n- a list\n---\n")


def test_unknown_top_level_field_preserved() -> None:
    m, _ = parse_skill_text("---\nname: foo\ndescription: bar\nweird: 42\n---\n")
    assert m.model_dump().get("weird") == 42


def test_slug_validation_helper() -> None:
    assert is_valid_slug("kebab-case")
    assert is_valid_slug("v1")
    assert not is_valid_slug("")
    assert not is_valid_slug("-leading")
    assert not is_valid_slug("trailing-")
    assert not is_valid_slug("has space")
    assert not is_valid_slug("a/b")
