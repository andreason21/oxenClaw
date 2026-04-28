"""Skill inventory: every shipped SKILL.md must parse."""

from __future__ import annotations

from pathlib import Path

import pytest

from oxenclaw.clawhub.frontmatter import SkillManifestError, parse_skill_file

SKILLS_DIR = Path(__file__).parent.parent / "oxenclaw" / "skills"


def _all_skill_dirs() -> list[Path]:
    if not SKILLS_DIR.is_dir():
        return []
    return sorted(p for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").exists())


@pytest.mark.parametrize("skill_dir", _all_skill_dirs(), ids=lambda p: p.name)
def test_every_skill_has_valid_frontmatter(skill_dir: Path) -> None:
    """Every directory under oxenclaw/skills/ with a SKILL.md must
    parse cleanly. Catches drift between openclaw imports and our
    SkillManifest schema.
    """
    skill_md = skill_dir / "SKILL.md"
    try:
        manifest, body = parse_skill_file(skill_md)
    except SkillManifestError as exc:
        pytest.fail(f"{skill_dir.name}: parse failed — {exc}")
    assert manifest.name
    assert manifest.description
    assert body.strip(), f"{skill_dir.name}: body is empty"


def test_at_least_50_skills_shipped() -> None:
    """Sanity: after the openclaw bulk-import we should have ≥50
    skills available."""
    assert len(_all_skill_dirs()) >= 50, "expected the openclaw skill bundle to be present"
