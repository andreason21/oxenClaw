"""coding_agent tool — delegate to a CLI coding agent in an ephemeral workspace.

Mirrors openclaw's `skills/coding-agent` SKILL.md behaviour. Detects which
CLI is installed (claude / codex / opencode / pi), invokes it inside a
fresh workspace dir from `prepare_skill_runtime`, and returns the CLI's
final stdout (truncated for context safety).

Each CLI gets a thin invocation adapter so the tool can normalise the
prompt across them. The list is small; new adapters are easy.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.clawhub.loader import InstalledSkill
from oxenclaw.clawhub.runtime import prepare_skill_runtime
from oxenclaw.config.paths import OxenclawPaths, default_paths
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("tools.coding")

CliName = Literal["claude", "codex", "opencode", "pi", "auto"]


@dataclass(frozen=True)
class CliAdapter:
    name: str
    bin: str
    build_argv: BuildArgvFn


# `(prompt, workspace) -> argv` — keeps adapter authoring trivial.
from collections.abc import Callable

BuildArgvFn = Callable[[str, Path], list[str]]


def _argv_claude(prompt: str, ws: Path) -> list[str]:
    # `claude --print` is non-interactive; permission bypass enables full tools.
    return [
        "claude",
        "--permission-mode",
        "bypassPermissions",
        "--print",
        prompt,
    ]


def _argv_codex(prompt: str, ws: Path) -> list[str]:
    return ["codex", "exec", prompt]


def _argv_opencode(prompt: str, ws: Path) -> list[str]:
    return ["opencode", "run", "--prompt", prompt]


def _argv_pi(prompt: str, ws: Path) -> list[str]:
    return ["pi", "run", "--prompt", prompt]


_ADAPTERS: dict[str, CliAdapter] = {
    "claude": CliAdapter("claude", "claude", _argv_claude),
    "codex": CliAdapter("codex", "codex", _argv_codex),
    "opencode": CliAdapter("opencode", "opencode", _argv_opencode),
    "pi": CliAdapter("pi", "pi", _argv_pi),
}


def detect_available_clis() -> list[str]:
    """Return CLI names whose binary is on PATH, in preference order."""
    return [name for name, ad in _ADAPTERS.items() if shutil.which(ad.bin) is not None]


def _select_cli(requested: CliName) -> CliAdapter | None:
    if requested != "auto":
        ad = _ADAPTERS.get(requested)
        if ad and shutil.which(ad.bin):
            return ad
        return None
    for name in ("claude", "codex", "opencode", "pi"):
        ad = _ADAPTERS[name]
        if shutil.which(ad.bin):
            return ad
    return None


@dataclass
class _RunOutcome:
    cli: str
    exit_code: int
    stdout: str
    stderr: str
    truncated_stdout: bool


async def _run_in_workspace(
    adapter: CliAdapter,
    prompt: str,
    workspace: Path,
    *,
    env: dict[str, str],
    timeout_seconds: float,
    max_stdout_chars: int,
) -> _RunOutcome:
    argv = adapter.build_argv(prompt, workspace)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(workspace),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        return _RunOutcome(adapter.name, -1, "", f"missing binary: {exc}", False)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        proc.kill()
        out, err = await proc.communicate()
    stdout = (out or b"").decode("utf-8", errors="replace")
    stderr = (err or b"").decode("utf-8", errors="replace")
    truncated = len(stdout) > max_stdout_chars
    if truncated:
        stdout = (
            stdout[:max_stdout_chars] + f"\n[...truncated {len(stdout) - max_stdout_chars} chars]"
        )
    return _RunOutcome(
        cli=adapter.name,
        exit_code=proc.returncode or 0,
        stdout=stdout,
        stderr=stderr[-2000:],
        truncated_stdout=truncated,
    )


class _CodingArgs(BaseModel):
    task: str = Field(..., description="The coding task in plain language.")
    cli: CliName = Field("auto", description="Which CLI to use; 'auto' picks the first available.")
    timeout_seconds: float = Field(300.0, description="Hard cap on the CLI run.", gt=0)
    max_stdout_chars: int = Field(16_000, description="Char budget for the captured stdout.", gt=0)


def coding_agent_tool(
    *,
    skill: InstalledSkill | None = None,
    paths: OxenclawPaths | None = None,
) -> Tool:
    """Build the `coding_agent` tool.

    `skill` may be omitted — the tool falls back to a synthetic skill so it
    can run before the user has installed the SKILL.md (workspace defaults
    apply: ephemeral, no env_overrides).
    """
    paths = paths or default_paths()

    async def _h(args: _CodingArgs) -> str:
        adapter = _select_cli(args.cli)
        if adapter is None:
            available = detect_available_clis() or "(none)"
            return (
                f"coding_agent error: no CLI available; requested={args.cli!r}, on PATH={available}"
            )

        # Pick a runtime: real skill if provided, else synthetic minimal one.
        skill_to_use = skill or _synthetic_skill()
        with prepare_skill_runtime(skill_to_use, paths=paths) as rt:
            outcome = await _run_in_workspace(
                adapter,
                args.task,
                rt.workspace_dir,
                env=rt.env,
                timeout_seconds=args.timeout_seconds,
                max_stdout_chars=args.max_stdout_chars,
            )
            if outcome.exit_code != 0:
                rt.mark_failed()
                return (
                    f"coding_agent[{outcome.cli}] exit={outcome.exit_code}\n"
                    f"stderr:\n{outcome.stderr or '(empty)'}\n"
                    f"stdout:\n{outcome.stdout or '(empty)'}"
                )
            return (
                f"coding_agent[{outcome.cli}] ok\n"
                f"workspace: {rt.workspace_dir}\n"
                f"---\n{outcome.stdout}"
            )

    return FunctionTool(
        name="coding_agent",
        description=(
            "Delegate a coding task to a CLI coding agent (claude / codex / "
            "opencode / pi). Runs in an ephemeral workspace and returns the "
            "CLI's stdout."
        ),
        input_model=_CodingArgs,
        handler=_h,
    )


def _synthetic_skill() -> InstalledSkill:
    """Used when no SKILL.md is on disk — minimal stand-in."""
    from oxenclaw.clawhub.frontmatter import parse_skill_text

    md = "---\nname: coding-agent\ndescription: synthetic\n---\n"
    manifest, body = parse_skill_text(md)
    return InstalledSkill(
        slug="coding-agent",
        manifest=manifest,
        skill_md_path=Path("/tmp/coding-agent/SKILL.md"),
        body=body,
        origin=None,
    )


__all__ = [
    "CliAdapter",
    "CliName",
    "coding_agent_tool",
    "detect_available_clis",
]
