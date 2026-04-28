"""Runtime skill activation — `/<slug>` slash-command → user message body.

Mirrors `hermes-agent/agent/skill_commands.py:306-385`. When the user
types `/<slug>` we load the matching skill, prepend an activation
banner that hints to the LLM "the user wants you to follow these
instructions for this turn", and append the skill directory + setup
hints so the model can find the supporting files.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from oxenclaw.clawhub.loader import InstalledSkill, load_installed_skills
from oxenclaw.clawhub.preprocessing import preprocess_skill_body
from oxenclaw.config.paths import OxenclawPaths


def _supporting_files(skill_dir: Path) -> list[str]:
    """Return absolute paths to non-SKILL.md files in `skill_dir` (top-level only)."""
    if not skill_dir.exists():
        return []
    out: list[str] = []
    for entry in sorted(skill_dir.iterdir()):
        if entry.is_dir():
            continue
        if entry.name == "SKILL.md":
            continue
        if entry.name.startswith("."):
            continue
        out.append(str(entry))
    return out


def build_skill_invocation_message(
    slug: str,
    user_instruction: str = "",
    *,
    paths: OxenclawPaths | None = None,
    skills: Iterable[InstalledSkill] | None = None,
    session_id: str = "",
) -> str | None:
    """Render the user-message body for a `/<slug>` invocation.

    Returns `None` if no installed skill matches `slug`.
    """
    candidates = list(skills) if skills is not None else load_installed_skills(paths)
    match: InstalledSkill | None = None
    for s in candidates:
        if s.slug == slug or s.name == slug:
            match = s
            break
    if match is None:
        return None

    skill_dir = match.skill_md_path.parent
    body = preprocess_skill_body(
        match.body,
        skill_dir=skill_dir,
        session_id=session_id,
    )

    activation = (
        f'[IMPORTANT: The user has invoked the "{slug}" skill. '
        "Treat the body below as instructions to follow for THIS turn.]"
    )
    parts: list[str] = [activation, "", body.strip()]
    if user_instruction.strip():
        parts.extend(["", f"User instruction: {user_instruction.strip()}"])
    parts.extend(["", f"[Skill directory: {skill_dir}]"])
    files = _supporting_files(skill_dir)
    if files:
        parts.append("[Supporting files:]")
        for f in files:
            parts.append(f"  - {f}")
    setup_hint = match.manifest.openclaw.requires
    if setup_hint and (setup_hint.bins or setup_hint.env):
        parts.append("[Setup hints:]")
        if setup_hint.bins:
            parts.append(f"  required bins: {', '.join(setup_hint.bins)}")
        if setup_hint.env:
            parts.append(f"  required env: {', '.join(setup_hint.env)}")
    return "\n".join(parts)


def detect_skill_slash_command(text: str) -> tuple[str, str] | None:
    """Return `(slug, remaining_text)` if `text` starts with `/<slug>`, else None."""
    stripped = (text or "").lstrip()
    if not stripped.startswith("/"):
        return None
    head, _, rest = stripped.partition(" ")
    slug = head[1:].strip()
    if not slug:
        return None
    # Reject anything with whitespace / disallowed chars by way of validating
    # against the skill manifest slug rules.
    if not all(c.isalnum() or c in "-_" for c in slug):
        return None
    return slug, rest.strip()


__all__ = ["build_skill_invocation_message", "detect_skill_slash_command"]
