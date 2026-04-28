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

Operator UX hooks:
- `~/.oxenclaw/skills_config.json`:
  ``{"disabled": [...], "platform_disabled": {<slug>: ["macos"]},
     "external_dirs": ["..."]}`` (JSON; YAML supported via `yaml` if
  installed and the file ends in `.yaml`).
- Frontmatter `platforms: [linux, macos, windows]` filters out skills
  that don't match the host platform.
"""

from __future__ import annotations

import json
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def _host_platform() -> str:
    """Normalise platform.system() to {"linux","macos","windows"}."""
    sysname = platform.system().lower()
    if sysname == "darwin":
        return "macos"
    if sysname == "linux":
        return "linux"
    if sysname.startswith("win"):
        return "windows"
    return sysname


def _read_skills_config(paths: OxenclawPaths) -> dict[str, Any]:
    """Read the optional `~/.oxenclaw/skills_config.json` (or `.yaml`).

    Returns an empty dict when the file is missing / malformed.
    """
    json_path = paths.home / "skills_config.json"
    yaml_path = paths.home / "skills_config.yaml"
    raw_text: str | None = None
    if json_path.exists():
        try:
            raw_text = json_path.read_text(encoding="utf-8")
            data = json.loads(raw_text)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("skills_config.json unreadable: %s", exc)
            return {}
    if yaml_path.exists():
        try:
            import yaml  # local import — yaml may not be on every install

            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.warning("skills_config.yaml unreadable: %s", exc)
            return {}
    return {}


def _platforms_from_manifest(manifest: SkillManifest) -> list[str]:
    """Pluck `platforms: [...]` from the raw manifest extras (if any)."""
    extras = manifest.model_extra or {}
    raw = extras.get("platforms")
    if isinstance(raw, list):
        return [str(p).lower() for p in raw if isinstance(p, str)]
    # Also support metadata.openclaw.os (existing field).
    oc_os = manifest.openclaw.os if manifest.openclaw else []
    if oc_os:
        return [p.lower() for p in oc_os]
    return []


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


def _skills_in_dir(
    root: Path,
    *,
    host_platform: str | None = None,
    disabled: set[str] | None = None,
    platform_disabled: dict[str, list[str]] | None = None,
) -> list[InstalledSkill]:
    """Parse every `SKILL.md` directly under `root/<slug>/`.

    `host_platform` filters out skills whose `platforms:` list doesn't
    match. `disabled` skips skills whose slug is in the set.
    `platform_disabled[<slug>]` skips a skill on listed platforms.
    """
    if not root.exists():
        return []
    hp = (host_platform or _host_platform()).lower()
    disabled = disabled or set()
    platform_disabled = platform_disabled or {}
    out: list[InstalledSkill] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.exists():
            continue
        if entry.name in disabled:
            continue
        try:
            manifest, body = parse_skill_file(skill_md)
        except SkillManifestError as exc:
            logger.warning("skipping malformed skill %s: %s", entry.name, exc)
            continue
        platforms = _platforms_from_manifest(manifest)
        if platforms and hp not in platforms:
            continue
        per_skill_block = platform_disabled.get(entry.name) or []
        if hp in [str(p).lower() for p in per_skill_block]:
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

    Honours `~/.oxenclaw/skills_config.{json,yaml}`:
      ``disabled``: list of slugs to hide entirely.
      ``platform_disabled``: per-slug platform skip list.
      ``external_dirs``: extra scan roots appended after the user dir.
    """
    resolved = paths or default_paths()
    config = _read_skills_config(resolved)
    raw_disabled = config.get("disabled") or []
    disabled = {str(s) for s in raw_disabled if isinstance(s, str)}
    raw_pdis = config.get("platform_disabled") or {}
    platform_disabled: dict[str, list[str]] = {}
    if isinstance(raw_pdis, dict):
        for slug, items in raw_pdis.items():
            if isinstance(slug, str) and isinstance(items, list):
                platform_disabled[slug] = [str(x) for x in items if isinstance(x, str)]
    raw_external = config.get("external_dirs") or []
    external_dirs: list[Path] = []
    if isinstance(raw_external, list):
        for d in raw_external:
            if isinstance(d, str) and d.strip():
                external_dirs.append(Path(d).expanduser())

    hp = _host_platform()
    user_root = resolved.home / "skills"
    user_skills = _skills_in_dir(
        user_root,
        host_platform=hp,
        disabled=disabled,
        platform_disabled=platform_disabled,
    )
    external_skills: list[InstalledSkill] = []
    for d in external_dirs:
        external_skills.extend(
            _skills_in_dir(
                d,
                host_platform=hp,
                disabled=disabled,
                platform_disabled=platform_disabled,
            )
        )
    if not include_bundled:
        merged = user_skills + [
            e for e in external_skills if e.slug not in {s.slug for s in user_skills}
        ]
        merged.sort(key=lambda s: s.slug)
        return merged
    bundled = _skills_in_dir(
        _BUNDLED_SKILLS_DIR,
        host_platform=hp,
        disabled=disabled,
        platform_disabled=platform_disabled,
    )
    seen_slugs = {s.slug for s in user_skills}
    merged = list(user_skills)
    for e in external_skills:
        if e.slug not in seen_slugs:
            merged.append(e)
            seen_slugs.add(e.slug)
    for b in bundled:
        if b.slug not in seen_slugs:
            merged.append(b)
            seen_slugs.add(b.slug)
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
