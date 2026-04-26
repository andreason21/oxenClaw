"""skill_creator tool — scaffold a new SKILL.md into the skills dir.

Mirrors openclaw `skills/skill-creator`. Writes a minimal but valid
frontmatter block + optional python tool stub. The loader picks up the
new skill on next `load_installed_skills()` call.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.clawhub.frontmatter import is_valid_slug, parse_skill_text
from oxenclaw.config.paths import OxenclawPaths, default_paths

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def slugify(name: str) -> str:
    s = name.strip().lower().replace("_", "-")
    s = _SLUG_RE.sub("-", s).strip("-")
    return s or "unnamed-skill"


_SKILL_TEMPLATE = """\
---
name: {name}
description: {description!r}
homepage: {homepage}
openclaw:
  emoji: {emoji!r}
{env_block}{install_block}---

# {name}

{body}
"""

_TOOL_STUB = '''\
"""Auto-generated stub for skill {slug!r}.

Replace the handler with real logic. Register the tool on a
oxenclaw ToolRegistry so the agent can call it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from oxenclaw.agents.tools import FunctionTool, Tool


class _Args(BaseModel):
    text: str = Field(..., description="Stub input — replace.")


def {fn_name}_tool() -> Tool:
    async def _h(args: _Args) -> str:
        return f"{slug}: stub got {{args.text!r}}"
    return FunctionTool(
        name="{fn_name}",
        description="Stub for the {slug} skill.",
        input_model=_Args,
        handler=_h,
    )
'''


class _CreateArgs(BaseModel):
    name: str = Field(..., description="Human-readable skill name.")
    description: str = Field(..., description="One-line summary.")
    body: str = Field(
        "(Describe how to use this skill.)",
        description="Markdown body that follows the frontmatter.",
    )
    homepage: str = Field("https://github.com/oxenclaw")
    emoji: str = Field("✨")
    env_overrides: dict[str, str] | None = Field(
        None, description="Optional env vars (`$VAR` references expand)."
    )
    install_anybins: list[str] | None = Field(
        None, description="Optional: list of binaries any of which satisfies."
    )
    write_tool_stub: bool = Field(
        False, description="Also write a Python tool stub alongside SKILL.md."
    )
    overwrite: bool = Field(False, description="Overwrite existing skill dir.")


def _format_env_block(env: dict[str, str] | None) -> str:
    if not env:
        return ""
    lines = ["  env_overrides:"]
    for k, v in env.items():
        lines.append(f"    {k}: {v!r}")
    return "\n".join(lines) + "\n"


def _format_install_block(bins: list[str] | None) -> str:
    if not bins:
        return ""
    lines = ["  requires:", "    anyBins:"]
    for b in bins:
        lines.append(f"      - {b}")
    return "\n".join(lines) + "\n"


def skill_creator_tool(*, paths: OxenclawPaths | None = None) -> Tool:
    paths = paths or default_paths()

    async def _h(args: _CreateArgs) -> str:
        slug = slugify(args.name)
        if not is_valid_slug(slug):
            return f"skill_creator error: derived slug {slug!r} is invalid"
        target_dir = paths.home / "skills" / slug
        if target_dir.exists() and not args.overwrite:
            return (
                f"skill_creator error: {target_dir} already exists (pass overwrite=true to replace)"
            )
        target_dir.mkdir(parents=True, exist_ok=True)
        skill_md = target_dir / "SKILL.md"
        rendered = _SKILL_TEMPLATE.format(
            name=slug,
            description=args.description,
            homepage=args.homepage,
            emoji=args.emoji,
            env_block=_format_env_block(args.env_overrides),
            install_block=_format_install_block(args.install_anybins),
            body=args.body,
        )
        # Validate before writing.
        try:
            parse_skill_text(rendered)
        except Exception as exc:
            return f"skill_creator error: generated frontmatter invalid: {exc}"
        skill_md.write_text(rendered, encoding="utf-8")

        files_written = [str(skill_md)]
        if args.write_tool_stub:
            fn_name = slug.replace("-", "_")
            stub_path = target_dir / f"{fn_name}.py"
            stub_path.write_text(_TOOL_STUB.format(slug=slug, fn_name=fn_name), encoding="utf-8")
            files_written.append(str(stub_path))
        return f"skill_creator ok: wrote {len(files_written)} file(s)\n" + "\n".join(
            f"  {f}" for f in files_written
        )

    return FunctionTool(
        name="skill_creator",
        description=(
            "Scaffold a new skill into ~/.oxenclaw/skills/<slug>/. "
            "Writes a valid SKILL.md and (optionally) a Python tool stub."
        ),
        input_model=_CreateArgs,
        handler=_h,
    )


__all__ = ["skill_creator_tool", "slugify"]
