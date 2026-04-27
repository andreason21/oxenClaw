"""Skill loader + agent-prompt formatter.

Two source directories are merged:

1. **Bundled** (`oxenclaw/skills/<slug>/SKILL.md`) — the curated skills
   shipped with the package (weather, summarize, github, healthcheck,
   session_logs, skill_creator). These load even on a fresh install with
   no `~/.oxenclaw/` config so the model knows about them out of the
   box.
2. **User-installed** (`~/.oxenclaw/skills/<slug>/SKILL.md`) — anything
   the operator wrote or pulled from ClawHub. These take precedence
   over a bundled skill of the same slug (lets users override).

Output is rendered into the openclaw-shaped `<available_skills>` block
that agents prepend to their system prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from oxenclaw.clawhub.frontmatter import (
    SkillManifest,
    SkillManifestError,
    parse_skill_file,
)
from oxenclaw.clawhub.lockfile import OriginMetadata
from oxenclaw.config.paths import OxenclawPaths, default_paths
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("clawhub.loader")

# `oxenclaw/skills/` lives next to this loader's parent (`oxenclaw/`).
_BUNDLED_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


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


def _skills_in_dir(root: Path) -> list[InstalledSkill]:
    """Parse every `SKILL.md` directly under `root/<slug>/`."""
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


def load_installed_skills(
    paths: OxenclawPaths | None = None,
    *,
    include_bundled: bool = True,
) -> list[InstalledSkill]:
    """Return bundled + user-installed skills, deduped by slug.

    User skills (`~/.oxenclaw/skills/`) win over a bundled skill of the
    same name so operators can override behaviour by writing their own
    `weather/SKILL.md` for example.
    """
    resolved = paths or default_paths()
    user_root = resolved.home / "skills"
    user_skills = _skills_in_dir(user_root)
    if not include_bundled:
        return user_skills
    bundled = _skills_in_dir(_BUNDLED_SKILLS_DIR)
    user_slugs = {s.slug for s in user_skills}
    merged = user_skills + [b for b in bundled if b.slug not in user_slugs]
    merged.sort(key=lambda s: s.slug)
    return merged


def format_skills_for_prompt(skills: list[InstalledSkill]) -> str:
    """Render the openclaw-shaped XML block agents append to system prompts.

    Empty-list returns "" so callers can blindly concatenate.

    The leading `<usage>` note is critical: skills are reference
    material, not function-calling tools. Without this hint LLMs
    cargo-cult the skill `<name>` into a `tool_use` block and the
    pi-runtime returns `tool {name!r} is not registered` (e.g. the
    clawhub `stock-analysis` skill ships a `commands:` list in its
    frontmatter that looks tool-shaped to the model). Steer the model
    toward reading SKILL.md and invoking the documented scripts via
    the shell tool instead.
    """
    if not skills:
        return ""
    lines = [
        "<available_skills>",
        "  <usage>Skills are documentation, NOT callable tools. Do not emit a "
        "tool_use block named after a skill — there is no function with "
        "that name registered. To use a skill: read SKILL.md at its "
        "&lt;location&gt;, then run the scripts it documents via the "
        "shell tool (most skills ship under &lt;location&gt;/scripts/). "
        "If the user's request implies a domain not covered by the skills "
        "listed below, call skill_resolver(query=&quot;...&quot;) — it is a "
        "real callable tool that searches ClawHub, installs the best match, "
        "and returns the SKILL.md path + usage instructions.</usage>",
    ]
    for s in skills:
        lines.append("  <skill>")
        lines.append(f"    <name>{_xml_escape(s.name)}</name>")
        lines.append(f"    <description>{_xml_escape(s.description)}</description>")
        lines.append(f"    <location>{_xml_escape(str(s.skill_md_path))}</location>")
        lines.append("  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
