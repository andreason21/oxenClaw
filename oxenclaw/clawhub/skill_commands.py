"""Skill commands → auto-registered LLM tools.

When a SKILL.md frontmatter declares a `commands:` block, every entry
becomes a callable tool the agent can invoke. The handler renders the
shell template with the model-provided inputs, runs it via the
existing shell-tool sandbox, and returns the trimmed stdout.

Mirrors openclaw `auto-reply/skill-commands.ts`. Simpler — we lean on
the gateway's existing `ShellTool` for execution, so this module only
covers (a) frontmatter → Pydantic input model conversion, and
(b) safe template rendering.
"""

from __future__ import annotations

import asyncio
import shlex
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.clawhub.frontmatter import SkillCommand
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("clawhub.skill_commands")


# Map declared input types → Python annotations for create_model.
_TYPE_MAP: dict[str, type] = {
    "string": str,
    "str": str,
    "int": int,
    "integer": int,
    "float": float,
    "number": float,
    "bool": bool,
    "boolean": bool,
}


def _build_input_model(cmd: SkillCommand, *, model_name: str) -> type[BaseModel]:
    """Synthesise a Pydantic model for the command's `inputs:` block."""
    field_defs: dict[str, tuple[Any, Any]] = {}
    for key, spec in cmd.inputs.items():
        ann = _TYPE_MAP.get((spec.get("type") or "string").lower(), str)
        required = bool(spec.get("required", False))
        default = spec.get("default")
        description = spec.get("description") or f"Argument `{key}`"
        if required:
            field_defs[key] = (ann, Field(..., description=description))
        else:
            field_defs[key] = (ann | None, Field(default=default, description=description))
    config = ConfigDict(extra="forbid")
    return create_model(  # type: ignore[no-any-return]
        model_name,
        __config__=config,
        **field_defs,
    )


def _render_template(template: str, args: dict[str, Any]) -> str:
    """Substitute `{name}` placeholders with shell-quoted args.

    SAFETY: every value is `shlex.quote`d before substitution so a
    malicious model can't inject `; rm -rf /` via an input value.
    Templates that need raw substitution (rare) can use `{!raw:name}`
    which passes the value through verbatim. Use sparingly — the
    template author owns the risk."""
    out = template
    for key, value in args.items():
        if value is None:
            continue
        text = str(value)
        # Raw escape hatch.
        raw_marker = f"{{!raw:{key}}}"
        if raw_marker in out:
            out = out.replace(raw_marker, text)
        out = out.replace("{" + key + "}", shlex.quote(text))
    return out


async def _run_shell(rendered: str, *, timeout_seconds: float) -> tuple[int, str, str]:
    """Run via /bin/sh; capture stdout/stderr.

    We deliberately use the lightweight asyncio subprocess path here
    instead of the gateway's `ShellTool`, because skill commands run
    in a less-restricted context — they're declared by trusted
    operators (the skill author + installer), not by an arbitrary
    LLM emission. The shell tool's full sandbox stays available
    behind `shell` for model-driven commands."""
    proc = await asyncio.create_subprocess_shell(
        rendered,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        return -1, "", f"timeout after {timeout_seconds}s"
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


def build_skill_command_tool(
    skill_name: str, cmd: SkillCommand, *, max_output_chars: int = 4000
) -> Tool:
    """Return a Tool whose handler renders the template + runs it.

    `skill_name` is prefixed onto the tool registration name to avoid
    collisions across skills (`weather.weather_lookup` rather than
    bare `weather_lookup`)."""
    qualified_name = f"{skill_name}.{cmd.name}"
    input_model = _build_input_model(cmd, model_name=f"_SC_{skill_name}_{cmd.name}")

    async def _h(args: BaseModel) -> str:
        kwargs = args.model_dump()
        rendered = _render_template(cmd.template, kwargs)
        logger.info(
            "skill-command exec: skill=%s cmd=%s args=%s",
            skill_name,
            cmd.name,
            sorted(kwargs.keys()),
        )
        rc, stdout, stderr = await _run_shell(rendered, timeout_seconds=cmd.timeout_seconds)
        body = stdout if rc == 0 else f"{stdout}\n[stderr]\n{stderr}"
        if len(body) > max_output_chars:
            body = body[:max_output_chars] + "\n[...truncated]"
        prefix = f"$ {rendered}\n" if rc != 0 else ""
        return f"{prefix}{body}".rstrip() or f"(skill {skill_name}.{cmd.name} returned no output)"

    return FunctionTool(
        name=qualified_name,
        description=cmd.description,
        input_model=input_model,
        handler=_h,
    )


def build_skill_command_tools(skill_name: str, commands: list[SkillCommand]) -> list[Tool]:
    """Convenience: build one tool per declared command."""
    return [build_skill_command_tool(skill_name, c) for c in commands]


def maybe_route_slash_command(
    text: str,
    *,
    paths: Any | None = None,
    session_id: str = "",
) -> str | None:
    """If `text` starts with `/<slug>` and the slug matches a local
    skill, return the rendered activation message body — otherwise
    return None.

    Callers that own user-message ingestion (chat.send / dispatch) can
    use this as a hook before the message reaches `run_agent_turn`.
    """
    from oxenclaw.clawhub.activation import (
        build_skill_invocation_message,
        detect_skill_slash_command,
    )

    parsed = detect_skill_slash_command(text or "")
    if parsed is None:
        return None
    slug, remaining = parsed
    return build_skill_invocation_message(slug, remaining, paths=paths, session_id=session_id)


__all__ = [
    "build_skill_command_tool",
    "build_skill_command_tools",
    "maybe_route_slash_command",
]
