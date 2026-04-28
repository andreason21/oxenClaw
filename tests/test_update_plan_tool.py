"""Tests for the `update_plan` tool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.tools_pkg.update_plan_tool import update_plan_tool


def _paths(tmp: Path) -> OxenclawPaths:
    p = OxenclawPaths(home=tmp)
    p.ensure_home()
    return p


def _steps(statuses: list[str]) -> list[dict]:
    return [
        {"id": str(i + 1), "title": f"Step {i + 1}", "status": s, "notes": ""}
        for i, s in enumerate(statuses)
    ]


async def test_update_plan_writes_file_and_returns_summary(tmp_path: Path) -> None:
    """Call the tool and assert the file appears with correct contents and return shape."""
    paths = _paths(tmp_path)
    tool = update_plan_tool(paths=paths)

    result_str = await tool.execute(
        {
            "session_key": "sess1",
            "agent_id": "coding",
            "title": "My Plan",
            "steps": _steps(["completed", "in_progress", "pending"]),
        }
    )

    result = json.loads(result_str)
    assert result["ok"] is True
    assert result["completed"] == 1
    assert result["in_progress"] == 1
    assert result["pending"] == 1
    assert len(result["steps"]) == 3

    # File must exist on disk with the right content.
    plan_path = paths.plan_file("coding", "sess1")
    assert plan_path.exists()
    on_disk = json.loads(plan_path.read_text(encoding="utf-8"))
    assert on_disk["session_key"] == "sess1"
    assert on_disk["title"] == "My Plan"
    assert len(on_disk["steps"]) == 3
    assert on_disk["steps"][0]["status"] == "completed"
    assert "updated_at" in on_disk


async def test_update_plan_overwrites_atomically(tmp_path: Path) -> None:
    """Second call replaces the first plan; no leftover .tmp file."""
    paths = _paths(tmp_path)
    tool = update_plan_tool(paths=paths)

    await tool.execute(
        {
            "session_key": "sess2",
            "agent_id": "coding",
            "steps": _steps(["pending", "pending"]),
        }
    )
    await tool.execute(
        {
            "session_key": "sess2",
            "agent_id": "coding",
            "steps": _steps(["completed"]),
        }
    )

    plan_path = paths.plan_file("coding", "sess2")
    on_disk = json.loads(plan_path.read_text(encoding="utf-8"))
    # Only the second call's steps remain.
    assert len(on_disk["steps"]) == 1
    assert on_disk["steps"][0]["status"] == "completed"

    # No stray .tmp file.
    tmp_file = plan_path.with_name(plan_path.name + ".tmp")
    assert not tmp_file.exists()


async def test_update_plan_no_title_omits_key(tmp_path: Path) -> None:
    """When title is omitted the key should not appear in the stored file."""
    paths = _paths(tmp_path)
    tool = update_plan_tool(paths=paths)

    await tool.execute(
        {
            "session_key": "sess3",
            "agent_id": "coding",
            "steps": _steps(["pending"]),
        }
    )
    on_disk = json.loads(paths.plan_file("coding", "sess3").read_text(encoding="utf-8"))
    assert "title" not in on_disk


async def test_update_plan_rejects_empty_steps(tmp_path: Path) -> None:
    """An empty steps list must be rejected (min_length=1 on the model)."""
    paths = _paths(tmp_path)
    tool = update_plan_tool(paths=paths)

    with pytest.raises(Exception):
        await tool.execute({"session_key": "s", "agent_id": "coding", "steps": []})


async def test_update_plan_rejects_invalid_status(tmp_path: Path) -> None:
    """An unrecognised status literal must be rejected."""
    paths = _paths(tmp_path)
    tool = update_plan_tool(paths=paths)

    with pytest.raises(Exception):
        await tool.execute(
            {
                "session_key": "s",
                "agent_id": "coding",
                "steps": [{"id": "1", "title": "t", "status": "unknown", "notes": ""}],
            }
        )
