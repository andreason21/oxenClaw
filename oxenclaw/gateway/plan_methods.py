"""`plan.*` RPCs — read and list structured session plans.

Plans are written by the `update_plan` tool (``oxenclaw/tools_pkg/update_plan_tool.py``)
as JSON files alongside the session file.  These read-only methods let the
dashboard query plan state without touching the tool itself.

Methods
-------
plan.get(agent_id, session_key)
    Return the parsed plan file, or ``{"steps": []}`` when missing.

plan.list(agent_id=None)
    Walk the agent's sessions directory and return one summary row per
    ``*.plan.json`` file (session_key, step counts, updated_at).
    When ``agent_id`` is omitted, all agents are scanned.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from oxenclaw.config.paths import OxenclawPaths, default_paths
from oxenclaw.gateway.router import Router

# ---------------------------------------------------------------------------
# Param models
# ---------------------------------------------------------------------------


class _GetParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    session_key: str


class _ListParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_plan(path: Path) -> dict[str, Any]:
    """Read one plan file; return empty-steps sentinel on any error."""
    if not path.exists():
        return {"steps": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"steps": []}
    if not isinstance(data, dict):
        return {"steps": []}
    # Ensure steps key always present.
    data.setdefault("steps", [])
    return data


def _summarise(path: Path) -> dict[str, Any] | None:
    """Return a summary row for one plan file, or None on error."""
    data = _load_plan(path)
    if not data.get("steps") and not data.get("session_key"):
        return None

    counts: dict[str, int] = {
        "completed": 0,
        "in_progress": 0,
        "pending": 0,
        "blocked": 0,
        "cancelled": 0,
    }
    for step in data.get("steps", []):
        status = step.get("status", "pending")
        if status in counts:
            counts[status] += 1

    # Derive session_key from filename if not stored in file.
    session_key = data.get("session_key") or path.stem.replace(".plan", "")
    return {
        "session_key": session_key,
        "title": data.get("title"),
        "updated_at": data.get("updated_at"),
        **counts,
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_plan_methods(router: Router, *, paths: OxenclawPaths | None = None) -> None:
    """Register ``plan.get`` and ``plan.list`` on *router*."""
    resolved = paths or default_paths()

    @router.method("plan.get", _GetParams)
    async def _plan_get(p: _GetParams) -> dict[str, Any]:  # type: ignore[type-arg]
        return _load_plan(resolved.plan_file(p.agent_id, p.session_key))

    @router.method("plan.list", _ListParams)
    async def _plan_list(p: _ListParams) -> dict[str, Any]:  # type: ignore[type-arg]
        if p.agent_id:
            agent_dirs = [resolved.agent_dir(p.agent_id)]
        else:
            base = resolved.agents_dir
            agent_dirs = sorted(base.iterdir()) if base.exists() else []

        rows: list[dict[str, Any]] = []
        for adir in agent_dirs:
            sessions_dir = adir / "sessions"
            if not sessions_dir.exists():
                continue
            for plan_file in sorted(sessions_dir.glob("*.plan.json")):
                summary = _summarise(plan_file)
                if summary is not None:
                    rows.append({"agent_id": adir.name, **summary})

        return {"plans": rows}
