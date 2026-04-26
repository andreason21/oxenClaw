"""github tool — `gh` CLI delegation with verb allow-list.

Mirrors openclaw `skills/github`. The tool only allows curated read-side
verbs by default (operators can extend `allowed_verbs`). All output is
captured + truncated; stderr surfaces on non-zero exit.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from sampyclaw.agents.tools import FunctionTool, Tool

# Default allowlist — read-only operations only. Operators can extend
# this set when building the tool.
DEFAULT_ALLOWED_VERBS = (
    "issue list",
    "issue view",
    "pr list",
    "pr view",
    "pr diff",
    "pr checks",
    "repo view",
    "release view",
    "release list",
    "api",  # caller is responsible for picking GET endpoints
    "auth status",
)


class _GhArgs(BaseModel):
    verb: str = Field(..., description="Sub-command verb, e.g. 'issue list' or 'pr diff'.")
    args: list[str] = Field(
        default_factory=list,
        description="Positional + flag args appended after the verb.",
    )
    cwd: str | None = Field(None, description="Optional CWD (defaults to a tmpdir).")


@dataclass
class GithubToolConfig:
    allowed_verbs: tuple[str, ...] = field(default_factory=lambda: DEFAULT_ALLOWED_VERBS)
    timeout_seconds: float = 60.0
    max_stdout_chars: int = 24_000


def github_tool(config: GithubToolConfig | None = None) -> Tool:
    cfg = config or GithubToolConfig()

    async def _h(args: _GhArgs) -> str:
        if shutil.which("gh") is None:
            return "github error: `gh` CLI is not installed (see SKILL.md install)"
        verb = args.verb.strip()
        if verb not in cfg.allowed_verbs:
            return (
                f"github error: verb {verb!r} not in allow-list. "
                f"Allowed: {sorted(cfg.allowed_verbs)}"
            )
        # Forbid shell metacharacters in args — `gh` is invoked argv-style
        # but the model could still try to embed them.
        for a in args.args:
            if any(c in a for c in (";", "&&", "|", "`", "\n")):
                return f"github error: refused arg {a!r} (shell metacharacters)"

        argv = ["gh", *verb.split(), *args.args]
        # Inherit env so GH_TOKEN flows through.
        env = dict(os.environ)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=args.cwd or None,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            return f"github error: {exc}"
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=cfg.timeout_seconds)
        except TimeoutError:
            proc.kill()
            return f"github error: timed out after {cfg.timeout_seconds}s"
        stdout = (out or b"").decode("utf-8", errors="replace")
        stderr = (err or b"").decode("utf-8", errors="replace")
        if len(stdout) > cfg.max_stdout_chars:
            stdout = stdout[: cfg.max_stdout_chars] + "\n[...truncated]"
        if proc.returncode != 0:
            return (
                f"github exit={proc.returncode}\n"
                f"stderr:\n{stderr[-2000:]}\n"
                f"stdout:\n{stdout or '(empty)'}"
            )
        return stdout or "(empty output)"

    return FunctionTool(
        name="github",
        description=(
            "Run a curated GitHub CLI verb (`gh issue list`, `gh pr view`, "
            "`gh api ...`, ...). Read-only by default; operators can extend "
            "the allow-list."
        ),
        input_model=_GhArgs,
        handler=_h,
    )


__all__ = ["DEFAULT_ALLOWED_VERBS", "GithubToolConfig", "github_tool"]
