"""Skill loader + agent-prompt formatter.

Walks `~/.sampyclaw/skills/<slug>/SKILL.md`, parses each, and renders the
openclaw-shaped `<available_skills>` XML block that agents include in
their system prompt so the model knows which skills exist and where to
read them from.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sampyclaw.clawhub.frontmatter import (
    SkillManifest,
    SkillManifestError,
    parse_skill_file,
)
from sampyclaw.clawhub.lockfile import OriginMetadata
from sampyclaw.config.paths import SampyclawPaths, default_paths
from sampyclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("clawhub.loader")


@dataclass(frozen=True)
class InstalledSkill:
    slug: str
    manifest: SkillManifest
    skill_md_path: Path
    body: str
    origin: OriginMetadata | None

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def description(self) -> str:
        return self.manifest.description


def load_installed_skills(paths: SampyclawPaths | None = None) -> list[InstalledSkill]:
    """Walk the skills directory and parse every installed SKILL.md."""
    resolved = paths or default_paths()
    root = resolved.home / "skills"
    if not root.exists():
        return []
    out: list[InstalledSkill] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            manifest, body = parse_skill_file(skill_md)
        except SkillManifestError as exc:
            logger.warning("skipping malformed skill %s: %s", entry.name, exc)
            continue
        origin = OriginMetadata.load(entry / ".clawhub" / "origin.json")
        out.append(
            InstalledSkill(
                slug=entry.name,
                manifest=manifest,
                skill_md_path=skill_md,
                body=body,
                origin=origin,
            )
        )
    return out


def format_skills_for_prompt(skills: list[InstalledSkill]) -> str:
    """Render the openclaw-shaped XML block agents append to system prompts.

    Empty-list returns "" so callers can blindly concatenate.
    """
    if not skills:
        return ""
    lines = ["<available_skills>"]
    for s in skills:
        lines.append("  <skill>")
        lines.append(f"    <name>{_xml_escape(s.name)}</name>")
        lines.append(f"    <description>{_xml_escape(s.description)}</description>")
        lines.append(f"    <location>{_xml_escape(str(s.skill_md_path))}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
