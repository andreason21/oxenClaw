"""`skill_run` — execute a documented script from an installed skill.

ClawHub skills typically ship runnable scripts under
``<skill_dir>/scripts/`` and document them in SKILL.md. Pre-this-
tool, the agent could only `read_file` the SKILL.md to learn what
to call but had no way to actually run anything (the default tool
bundle has no shell). When a user asked "삼성전자 주가 알려줘" with
the `stock-analysis` skill installed, the model saw the skill in
``<available_skills>`` but produced text answers instead of firing.

`skill_run(skill, script, args)` is the missing executor. It:

  - Resolves ``<skill_dir>/scripts/<script>`` and refuses paths
    outside that directory (defense against `../` escape).
  - Picks an interpreter from the script extension:

      .py   →  ``uv run <script> …`` when uv is on PATH AND the
                script declares PEP 723 inline deps; otherwise
                ``python3 <script> …``.
      .sh   →  ``bash <script> …``
      .js   →  ``node <script> …``
      .ts   →  ``tsx <script> …`` (when tsx is on PATH)

  - Runs in the skill directory as cwd, captures stdout+stderr,
    truncates to keep the model context bounded, and surfaces a
    clear actionable error when the interpreter or required CLI
    is missing — feeding back into the compat-checker workflow
    so the user knows what to install.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.clawhub.loader import load_installed_skills
from oxenclaw.config.paths import OxenclawPaths, default_paths
from oxenclaw.plugin_sdk.runtime_env import get_logger
from oxenclaw.tools_pkg._desc import hermes_desc

logger = get_logger("tools.skill_run")


_OUTPUT_LIMIT = 8000  # chars of combined stdout+stderr before truncation


class _Args(BaseModel):
    skill: str = Field(
        ...,
        description=(
            "Slug of an installed skill (e.g. 'stock-analysis'). Use the "
            "name from the <available_skills> block, not the display name."
        ),
    )
    script: str = Field(
        ...,
        description=(
            "Script filename (relative to <skill_dir>/scripts/) — e.g. "
            "'analyze_stock.py'. Path traversal is rejected."
        ),
    )
    args: list[str] = Field(
        default_factory=list,
        description="Positional arguments passed to the script.",
    )
    timeout_seconds: int = Field(
        120,
        description="Max wall-clock seconds before the script is killed.",
        gt=0,
        le=600,
    )


def _resolve_script(skill_dir: Path, script: str) -> Path:
    scripts_dir = (skill_dir / "scripts").resolve()
    candidate = (scripts_dir / script).resolve()
    if scripts_dir not in candidate.parents and candidate != scripts_dir:
        raise ValueError(f"script {script!r} resolves outside the skill's scripts/ directory")
    if not candidate.is_file():
        raise FileNotFoundError(f"script not found: {candidate.relative_to(skill_dir)}")
    return candidate


def _has_pep723_header(script_path: Path) -> bool:
    """PEP 723 inline-deps scripts open with ``# /// script`` and end
    that block with ``# ///``. ``uv run`` understands them and
    auto-installs deps; bare python3 does not."""
    try:
        with script_path.open(encoding="utf-8", errors="replace") as f:
            head = f.read(2048)
    except OSError:
        return False
    return "# /// script" in head


def _build_argv(script_path: Path, args: list[str], which: Any) -> tuple[list[str], str | None]:
    """Pick an interpreter for `script_path`. Returns (argv, error_msg)
    where argv is None when no interpreter is available."""
    suffix = script_path.suffix.lower()
    if suffix == ".py":
        if which("uv") and _has_pep723_header(script_path):
            return (["uv", "run", str(script_path), *args], None)
        if which("python3"):
            return (["python3", str(script_path), *args], None)
        if which("python"):
            return (["python", str(script_path), *args], None)
        # Specific message because PEP 723 scripts NEED uv to install deps.
        if _has_pep723_header(script_path):
            return (
                [],
                "script declares PEP 723 inline deps but `uv` is not on PATH; "
                "install uv (`curl -LsSf https://astral.sh/uv/install.sh | sh`) "
                "or run the script in an environment where its deps are pre-installed",
            )
        return ([], "no python interpreter on PATH (need `python3` or `python`)")
    if suffix == ".sh":
        if which("bash"):
            return (["bash", str(script_path), *args], None)
        return ([], "no `bash` on PATH")
    if suffix == ".js":
        if which("node"):
            return (["node", str(script_path), *args], None)
        return ([], "no `node` on PATH")
    if suffix == ".ts":
        if which("tsx"):
            return (["tsx", str(script_path), *args], None)
        return ([], "no `tsx` on PATH (install with `npm i -g tsx`)")
    return ([], f"unsupported script extension: {suffix!r}")


def skill_run_tool(*, paths: OxenclawPaths | None = None) -> Tool:
    resolved_paths = paths

    async def _handler(args: _Args) -> str:
        p = resolved_paths or default_paths()
        try:
            installed = load_installed_skills(p)
        except Exception as exc:
            return f"skill_run error: failed to load installed skills: {exc}"
        match = next((s for s in installed if s.slug == args.skill), None)
        if match is None:
            available = ", ".join(sorted(s.slug for s in installed)) or "(none)"
            return (
                f"skill_run error: skill {args.skill!r} is not installed. "
                f"Installed skills: {available}. "
                "Use `skill_resolver(query=...)` to install one from ClawHub."
            )
        skill_dir = match.skill_md_path.parent
        try:
            script_path = _resolve_script(skill_dir, args.script)
        except (ValueError, FileNotFoundError) as exc:
            # Helpful hint: list what scripts ARE available.
            scripts_dir = skill_dir / "scripts"
            available = (
                sorted(p.name for p in scripts_dir.iterdir() if p.is_file())
                if scripts_dir.is_dir()
                else []
            )
            if available:
                avail_str = ", ".join(available)
                return f"skill_run error: {exc}. Available scripts: {avail_str}"
            # No scripts/ at all → almost certainly a knowledge-style skill.
            # Send the model back to the shell tool instead of letting it
            # paraphrase "no scripts" as "skill is broken" and refuse.
            return (
                f"skill_run error: {exc}. This skill ships no `scripts/` "
                "directory — it is knowledge-style and must be invoked via "
                "the `bash` (shell) tool using the CLI commands documented "
                f"in its <usage> block (look up {args.skill!r} in the "
                "<available_skills> system message). Do NOT call skill_run "
                "again for this skill."
            )

        argv, err = _build_argv(script_path, args.args, shutil.which)
        if err is not None:
            return f"skill_run error ({args.skill}/{args.script}): {err}"
        logger.info(
            "skill_run: skill=%s script=%s argv=%s timeout=%ds",
            args.skill,
            args.script,
            [*argv[:2], "…"] if len(argv) > 2 else argv,
            args.timeout_seconds,
        )

        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(skill_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=5.0,  # spawn timeout
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=args.timeout_seconds
            )
        except TimeoutError:
            return (
                f"skill_run error: {args.skill}/{args.script} timed out after "
                f"{args.timeout_seconds}s"
            )
        except FileNotFoundError as exc:
            return f"skill_run error: interpreter not found: {exc}"
        except Exception as exc:
            return f"skill_run error: subprocess failed: {exc}"

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        rc = proc.returncode

        # Compose output: stdout first (the model usually wants the
        # answer, not the noise), then stderr appended if it carries
        # signal. Truncate to keep the prompt context bounded.
        body = stdout
        if stderr.strip():
            body = body.rstrip() + "\n\n--- stderr ---\n" + stderr
        if len(body) > _OUTPUT_LIMIT:
            body = body[:_OUTPUT_LIMIT] + f"\n…(truncated, {len(body)} chars total)"
        prefix = f"[{args.skill}/{args.script} exit={rc}]"
        if rc != 0:
            return f"{prefix} non-zero exit\n{body}"
        return f"{prefix}\n{body}".rstrip()

    return FunctionTool(
        name="skill_run",
        description=hermes_desc(
            "Execute a documented script from an installed ClawHub skill. "
            "The script lives at <skill_dir>/scripts/<script>; arg shapes "
            "are documented in the SKILL.md body inside <available_skills>.",
            when_use=[
                "the request matches a skill listed in <available_skills>",
                "the SKILL.md body shows a runnable script for this task",
            ],
            when_skip=[
                "no installed skill matches (call skill_resolver first)",
                "you'd be running a generic shell command (use shell)",
                "you can answer from your own knowledge without scripts",
            ],
            alternatives={
                "skill_resolver": "find + install a skill from ClawHub",
                "shell": "ad-hoc shell commands not tied to a skill",
            },
            notes=(
                'Example: skill_run(skill="stock-analysis", '
                'script="analyze_stock.py", args=["AAPL"]). '
                "Path traversal outside scripts/ is rejected."
            ),
        ),
        input_model=_Args,
        handler=_handler,
    )


__all__ = ["skill_run_tool"]
