"""Tests for CodingAgent skeleton (P2-F first session).

Covers:
- factory routing (agent_type="coding" → CodingAgent)
- curated tool registry membership
- system prompt distinction from PiAgent default
- read_file / write_file / list_dir / search_files / shell tools in isolation
- approval gating wires correctly (gated_tool wraps write+shell)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from oxenclaw.agents.coding_agent import CODING_SYSTEM_PROMPT, CodingAgent
from oxenclaw.agents.factory import build_agent
from oxenclaw.agents.pi_agent import DEFAULT_SYSTEM_PROMPT as PI_DEFAULT_SYSTEM_PROMPT
from oxenclaw.agents.tools import ToolRegistry
from oxenclaw.tools_pkg.fs_tools import (
    list_dir_tool,
    read_file_tool,
    search_files_tool,
    shell_run_tool,
    write_file_tool,
)


# ── Factory routing ──────────────────────────────────────────────────────────


def test_factory_builds_coding_agent_when_requested() -> None:
    """build_agent(agent_type='coding') must return a CodingAgent instance."""
    agent = build_agent(agent_id="c", provider="anthropic", agent_type="coding")
    assert isinstance(agent, CodingAgent)


def test_factory_builds_pi_agent_by_default() -> None:
    """build_agent without agent_type must still return a plain PiAgent."""
    from oxenclaw.agents.pi_agent import PiAgent

    agent = build_agent(agent_id="p", provider="anthropic")
    # CodingAgent is a subclass of PiAgent; verify it's the base class.
    assert type(agent) is PiAgent  # noqa: E721


# ── Curated tool registry ────────────────────────────────────────────────────


def test_coding_agent_registers_curated_tools() -> None:
    """CodingAgent's _tools must contain the declared curated set."""
    agent = CodingAgent(agent_id="c")
    names = set(agent._tools.names())
    # Minimum required tools per the design doc.
    assert "read_file" in names
    assert "write_file" in names
    assert "shell" in names
    assert "list_dir" in names
    assert "edit" in names
    assert "grep" in names
    assert "glob" in names


def test_coding_agent_registry_now_includes_edit_grep_glob() -> None:
    """CodingAgent registry must include edit, grep, glob, read_pdf and exclude search_files."""
    agent = CodingAgent(agent_id="c")
    names = set(agent._tools.names())
    assert "edit" in names
    assert "grep" in names
    assert "glob" in names
    assert "read_pdf" in names
    # search_files is NOT in the curated registry (kept only as exported function).
    assert "search_files" not in names


def test_coding_agent_does_not_register_general_bundle() -> None:
    """CodingAgent must NOT include the general PiAgent default tools
    (get_time, echo) — the curated coding registry replaces them."""
    agent = CodingAgent(agent_id="c")
    names = set(agent._tools.names())
    assert "get_time" not in names
    assert "echo" not in names


def test_coding_agent_registry_is_isolated_when_tools_not_passed() -> None:
    """Two separately constructed CodingAgents must not share a registry."""
    a1 = CodingAgent(agent_id="c1")
    a2 = CodingAgent(agent_id="c2")
    assert a1._tools is not a2._tools


# ── System prompt ────────────────────────────────────────────────────────────


def test_coding_agent_uses_coding_system_prompt() -> None:
    """agent._system_prompt must differ from PiAgent's default and mention plan/edit."""
    agent = CodingAgent(agent_id="c")
    assert agent._system_prompt != PI_DEFAULT_SYSTEM_PROMPT
    low = agent._system_prompt.lower()
    assert "plan" in low or "edit" in low


def test_coding_agent_accepts_system_prompt_override() -> None:
    """CodingAgent should accept a custom system_prompt kwarg."""
    custom = "Custom coding instructions."
    agent = CodingAgent(agent_id="c", system_prompt=custom)
    assert agent._system_prompt == custom


# ── read_file tool ───────────────────────────────────────────────────────────


async def test_read_file_returns_content(tmp_path: Path) -> None:
    p = tmp_path / "hello.txt"
    p.write_text("hello world", encoding="utf-8")
    tool = read_file_tool()
    result = await tool.execute({"path": str(p)})
    assert "hello world" in result


async def test_read_file_missing_returns_error(tmp_path: Path) -> None:
    tool = read_file_tool()
    result = await tool.execute({"path": str(tmp_path / "nope.txt")})
    assert "error" in result.lower()


async def test_read_file_truncates_at_max_chars(tmp_path: Path) -> None:
    p = tmp_path / "big.txt"
    p.write_text("x" * 100, encoding="utf-8")
    tool = read_file_tool()
    result = await tool.execute({"path": str(p), "max_chars": 10})
    assert "truncated" in result
    assert len(result) < 150  # well under the original 100 chars + notice


# ── write_file tool ──────────────────────────────────────────────────────────


async def test_write_file_creates_file(tmp_path: Path) -> None:
    dest = tmp_path / "out.txt"
    tool = write_file_tool()
    result = await tool.execute({"path": str(dest), "content": "written"})
    assert dest.exists()
    assert dest.read_text() == "written"
    assert "wrote" in result


async def test_write_file_creates_parents(tmp_path: Path) -> None:
    dest = tmp_path / "sub" / "dir" / "file.txt"
    tool = write_file_tool()
    await tool.execute({"path": str(dest), "content": "nested"})
    assert dest.exists()


# ── list_dir tool ────────────────────────────────────────────────────────────


async def test_list_dir_returns_entries(tmp_path: Path) -> None:
    (tmp_path / "file.py").write_text("")
    (tmp_path / "subdir").mkdir()
    tool = list_dir_tool()
    result = await tool.execute({"path": str(tmp_path)})
    assert "file.py" in result
    assert "subdir/" in result


async def test_list_dir_missing_returns_error(tmp_path: Path) -> None:
    tool = list_dir_tool()
    result = await tool.execute({"path": str(tmp_path / "no_such_dir")})
    assert "error" in result.lower()


# ── search_files tool ────────────────────────────────────────────────────────


async def test_search_files_finds_pattern(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("hello")
    (tmp_path / "b.txt").write_text("world")
    tool = search_files_tool()
    result = await tool.execute({"root": str(tmp_path), "pattern": "*.py"})
    assert "a.py" in result
    assert "b.txt" not in result


async def test_search_files_contains_filter(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("needle here")
    (tmp_path / "b.py").write_text("nothing useful")
    tool = search_files_tool()
    result = await tool.execute(
        {"root": str(tmp_path), "pattern": "*.py", "contains": "needle"}
    )
    assert "a.py" in result
    assert "b.py" not in result


# ── shell tool ───────────────────────────────────────────────────────────────


async def test_shell_run_executes_command() -> None:
    tool = shell_run_tool()
    result = await tool.execute({"command": "echo hello_shell"})
    assert "hello_shell" in result


async def test_shell_run_captures_exit_code() -> None:
    tool = shell_run_tool()
    result = await tool.execute({"command": "exit 42"})
    assert "42" in result


async def test_shell_run_timeout() -> None:
    tool = shell_run_tool()
    result = await tool.execute({"command": "sleep 10", "timeout_seconds": 0.1})
    assert "timed out" in result.lower()


# ── Approval gating ──────────────────────────────────────────────────────────


def test_coding_agent_with_approval_manager_gates_write_and_shell() -> None:
    """When an ApprovalManager is injected, write_file and shell must be
    _GatedTool instances (description contains 'approval')."""
    from oxenclaw.approvals.manager import ApprovalManager

    mgr = ApprovalManager()
    agent = CodingAgent(agent_id="c", approval_manager=mgr)

    write_tool = agent._tools.get("write_file")
    shell_tool = agent._tools.get("shell")
    read_tool = agent._tools.get("read_file")

    assert write_tool is not None
    assert shell_tool is not None
    assert read_tool is not None

    # Gated tools surface "approval" in their description.
    assert "approval" in write_tool.description.lower()
    assert "approval" in shell_tool.description.lower()
    # read_file is never gated.
    assert "approval" not in read_tool.description.lower()


def test_coding_agent_without_approval_manager_does_not_gate() -> None:
    """Without an ApprovalManager the write/shell tools are undecorated."""
    agent = CodingAgent(agent_id="c")
    write_tool = agent._tools.get("write_file")
    # Undecorated write_file_tool description does not mention "requires human approval"
    # via gated_tool suffix ("requires human approval before execution").
    # It has the original description from write_file_tool(), which ends with
    # "REQUIRES human approval before execution." as documentation only, not gated.
    # So the distinguishing text from _GatedTool.description is:
    # "(requires human approval before execution)" — added by gated_tool.
    assert write_tool is not None
    # _GatedTool appends " (requires human approval before execution)" — the
    # parenthesised phrasing. The raw tool just mentions it in its own text.
    assert "(requires human approval before execution)" not in write_tool.description


# ── CODING_SYSTEM_PROMPT content checks ─────────────────────────────────────


def test_coding_system_prompt_mentions_plan() -> None:
    # The system prompt may use update_plan tool or a <plan> block — either counts.
    assert "plan" in CODING_SYSTEM_PROMPT.lower()


def test_coding_system_prompt_mentions_write_file() -> None:
    assert "write_file" in CODING_SYSTEM_PROMPT


def test_coding_system_prompt_mentions_approval() -> None:
    assert "approval" in CODING_SYSTEM_PROMPT.lower()
