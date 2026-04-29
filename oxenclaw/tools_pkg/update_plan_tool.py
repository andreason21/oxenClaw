"""`update_plan` tool — write or replace a structured plan for a session.

The LLM passes the **full** step list on every call; this module overwrites the
plan file atomically (write to a `.tmp` sibling, then `rename`).  Diff-based
updates were ruled out because they require the LLM to track previous state
precisely — full-replace is simpler to reason about and idempotent.

Return shape::

    {
        "ok": True,
        "steps": [...],        # echo of validated steps
        "completed": <int>,
        "in_progress": <int>,
        "pending": <int>,
    }

This gives the LLM immediate progress confirmation without a separate
`plan.get` round-trip.
"""

from __future__ import annotations

import json
import time
from typing import Literal

from pydantic import BaseModel, Field

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.config.paths import OxenclawPaths, default_paths
from oxenclaw.plugin_sdk.runtime_env import get_logger
from oxenclaw.tools_pkg._desc import hermes_desc

logger = get_logger("tools.update_plan")

# ---------------------------------------------------------------------------
# Pydantic models (strict — extra fields are rejected)
# ---------------------------------------------------------------------------

StatusLiteral = Literal["pending", "in_progress", "completed", "blocked", "cancelled"]


class _Step(BaseModel):
    model_config = {"extra": "forbid"}

    id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    status: StatusLiteral
    notes: str = ""


class _Args(BaseModel):
    model_config = {"extra": "forbid"}

    session_key: str = Field(..., min_length=1)
    agent_id: str = Field(..., min_length=1)
    steps: list[_Step] = Field(..., min_length=1)
    title: str | None = None


# ---------------------------------------------------------------------------
# Tool factory
# ---------------------------------------------------------------------------


def update_plan_tool(*, paths: OxenclawPaths | None = None) -> Tool:
    """Build the ``update_plan`` tool.

    Parameters
    ----------
    paths:
        Resolved filesystem paths.  Defaults to ``default_paths()`` when
        omitted so the tool can be registered without plumbing the paths
        object through every call site.
    """
    resolved = paths or default_paths()

    async def _handler(args: _Args) -> str:
        plan_path = resolved.plan_file(args.agent_id, args.session_key)
        plan_path.parent.mkdir(parents=True, exist_ok=True)

        steps_payload = [s.model_dump() for s in args.steps]
        payload: dict = {
            "session_key": args.session_key,
            "steps": steps_payload,
            "updated_at": time.time(),
        }
        if args.title is not None:
            payload["title"] = args.title

        # Atomic write: temp file in same directory → rename.
        tmp_path = plan_path.with_name(plan_path.name + ".tmp")
        try:
            tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp_path.rename(plan_path)
        except Exception:
            # Best-effort cleanup if rename failed.
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        logger.debug(
            "update_plan: wrote %d steps to %s",
            len(args.steps),
            plan_path,
        )

        counts: dict[str, int] = {"completed": 0, "in_progress": 0, "pending": 0}
        for step in args.steps:
            if step.status in counts:
                counts[step.status] += 1

        return json.dumps(
            {
                "ok": True,
                "steps": steps_payload,
                **counts,
            }
        )

    return FunctionTool(
        name="update_plan",
        description=hermes_desc(
            "Write or replace the structured plan for the current session. "
            "Pass the FULL step list each call — this overwrites the prior "
            "plan atomically.",
            when_use=[
                "the task has 3+ steps that benefit from tracking",
                "the dashboard should render live progress",
            ],
            when_skip=[
                "single-step trivial requests (overhead not worth it)",
                "you're storing freeform notes (use the wiki tools)",
            ],
            alternatives={"wiki": "long-form notes / decisions"},
            notes=(
                "status values: pending | in_progress | completed | blocked | "
                "cancelled. Only one in_progress at a time."
            ),
        ),
        input_model=_Args,
        handler=_handler,
    )


__all__ = ["update_plan_tool"]
