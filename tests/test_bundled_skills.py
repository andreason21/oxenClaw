"""Phase B1-B8: bundled-skill tool tests.

Each skill ships SKILL.md + a Python tool. We assert (1) the SKILL.md
parses cleanly via the existing frontmatter parser, and (2) the tool
behaves on at least the happy + error paths.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from sampyclaw.clawhub.frontmatter import parse_skill_file
from sampyclaw.config.paths import SampyclawPaths

# Skills under sampyclaw/skills/ — all should parse.
_BUNDLED = [
    "summarize",
    "weather",
    "github",
    "session_logs",
    "healthcheck",
    "skill_creator",
    "coding_agent",  # already shipped
]


def _skills_root() -> Path:
    return (
        Path(__file__).resolve().parent.parent
        / "sampyclaw"
        / "skills"
    )


@pytest.mark.parametrize("slug", _BUNDLED)
def test_bundled_skill_md_parses(slug: str) -> None:
    md = _skills_root() / slug / "SKILL.md"
    assert md.exists(), f"missing SKILL.md for {slug}"
    manifest, body = parse_skill_file(md)
    assert manifest.name
    assert manifest.description
    # The body shouldn't be empty (it's the doc surface for the LLM).
    assert body.strip()


# ─── summarize ──────────────────────────────────────────────────────


import sampyclaw.pi.providers  # noqa: F401
from sampyclaw.pi import (
    InMemoryAuthStorage,
    Model,
    StopEvent,
    TextDeltaEvent,
    register_provider_stream,
)
from sampyclaw.tools_pkg.summarize import summarize_tool


async def test_summarize_tool_calls_sub_llm() -> None:
    seen_messages: list = []

    async def fake(ctx, opts):  # type: ignore[no-untyped-def]
        seen_messages.append(ctx.messages[0].content)
        yield TextDeltaEvent(delta="(short summary)")
        yield StopEvent(reason="end_turn")

    register_provider_stream("sum_p", fake)
    model = Model(
        id="m-sum_p", provider="sum_p", max_output_tokens=64,
        extra={"base_url": "http://x"},
    )
    auth = InMemoryAuthStorage({"sum_p": "k"})  # type: ignore[dict-item]
    tool = summarize_tool(model=model, auth=auth)
    out = await tool.execute({"input_text": "hello world", "length": "short"})
    assert "short summary" in out
    assert "Reply in 1-2 sentences" in seen_messages[0]
    assert "hello world" in seen_messages[0]


async def test_summarize_tool_focus_appears_in_prompt() -> None:
    seen: list = []

    async def fake(ctx, opts):  # type: ignore[no-untyped-def]
        seen.append(ctx.messages[0].content)
        yield TextDeltaEvent(delta="ok")
        yield StopEvent(reason="end_turn")

    register_provider_stream("sum_p2", fake)
    model = Model(
        id="m-sum_p2", provider="sum_p2", max_output_tokens=64,
        extra={"base_url": "http://x"},
    )
    auth = InMemoryAuthStorage({"sum_p2": "k"})  # type: ignore[dict-item]
    tool = summarize_tool(model=model, auth=auth)
    await tool.execute(
        {"input_text": "doc", "length": "medium", "focus": "risks only"}
    )
    assert "Focus on: risks only" in seen[0]


# ─── weather ────────────────────────────────────────────────────────


from sampyclaw.tools_pkg.weather import weather_tool


def test_weather_tool_validation_requires_input() -> None:
    tool = weather_tool()
    # Pydantic validation raises before the handler runs — that's the
    # contract every FunctionTool relies on for typed input.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        asyncio.run(tool.execute({}))  # type: ignore[arg-type]


# ─── github ─────────────────────────────────────────────────────────


from sampyclaw.tools_pkg.github import github_tool


async def test_github_tool_blocks_non_allowlisted_verb(monkeypatch) -> None:
    import shutil as _sh

    # Pretend gh is installed so we exercise the allow-list check.
    monkeypatch.setattr(_sh, "which", lambda b: "/usr/bin/gh" if b == "gh" else None)
    tool = github_tool()
    out = await tool.execute({"verb": "secret action", "args": []})
    assert "not in allow-list" in out


async def test_github_tool_refuses_shell_metachar(monkeypatch) -> None:
    import shutil as _sh

    monkeypatch.setattr(_sh, "which", lambda b: "/usr/bin/gh" if b == "gh" else None)
    tool = github_tool()
    out = await tool.execute({"verb": "issue list", "args": ["--label", "x; rm -rf /"]})
    assert "shell metacharacters" in out


async def test_github_tool_missing_gh(monkeypatch) -> None:
    import shutil as _sh

    monkeypatch.setattr(_sh, "which", lambda _: None)
    tool = github_tool()
    out = await tool.execute({"verb": "issue list", "args": []})
    assert "not installed" in out


# ─── session_logs ───────────────────────────────────────────────────


from sampyclaw.pi import (
    AssistantMessage,
    CreateAgentSessionOptions,
    InMemorySessionManager,
    SystemMessage,
    TextContent,
    UserMessage,
)
from sampyclaw.tools_pkg.session_logs import session_logs_tool


async def test_session_logs_list_view_grep() -> None:
    sm = InMemorySessionManager()
    s = await sm.create(CreateAgentSessionOptions(agent_id="x", title="t1"))
    s.messages = [
        SystemMessage(content="be brief"),
        UserMessage(content="What about kraken?"),
        AssistantMessage(
            content=[TextContent(text="krakens are fascinating")],
            stop_reason="end_turn",
        ),
    ]
    await sm.save(s)
    tool = session_logs_tool(sm)

    out = await tool.execute({"action": "list"})
    assert s.id[:8] in out and "t1" in out

    out = await tool.execute({"action": "view", "session_id": s.id, "last_n": 5})
    assert "kraken" in out
    assert "session " in out

    out = await tool.execute({"action": "grep", "query": "kraken"})
    assert s.id[:8] in out
    assert "kraken" in out

    out = await tool.execute({"action": "grep", "query": "nothing-matches"})
    assert "no matches" in out


# ─── healthcheck ────────────────────────────────────────────────────


from sampyclaw.tools_pkg.healthcheck import healthcheck_tool


async def test_healthcheck_with_no_subsystems_still_runs() -> None:
    tool = healthcheck_tool()
    out = await tool.execute({})
    assert "healthcheck" in out
    assert "channels: (not wired)" in out
    assert "isolation:" in out


async def test_healthcheck_reports_session_count(tmp_path: Path) -> None:
    from sampyclaw.pi.persistence import SQLiteSessionManager

    sm = SQLiteSessionManager(tmp_path / "s.db")
    await sm.create(CreateAgentSessionOptions(agent_id="x"))
    tool = healthcheck_tool(sessions=sm)
    out = await tool.execute({})
    assert "sessions: 1 rows" in out
    sm.close()


# ─── skill_creator ──────────────────────────────────────────────────


from sampyclaw.clawhub.loader import load_installed_skills
from sampyclaw.tools_pkg.skill_creator import skill_creator_tool, slugify


def _paths(tmp_path: Path) -> SampyclawPaths:
    p = SampyclawPaths(home=tmp_path)
    p.ensure_home()
    return p


def test_slugify_normalises() -> None:
    assert slugify("My Cool Skill") == "my-cool-skill"
    assert slugify("foo_bar") == "foo-bar"
    assert slugify("###") == "unnamed-skill"


async def test_skill_creator_writes_parseable_skill(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    tool = skill_creator_tool(paths=paths)
    out = await tool.execute(
        {
            "name": "Hello World",
            "description": "Trivial demo skill.",
            "body": "Use this for demos.",
            "emoji": "👋",
        }
    )
    assert "skill_creator ok" in out
    skill_md = tmp_path / "skills" / "hello-world" / "SKILL.md"
    assert skill_md.exists()

    # The loader must be able to discover it.
    installed = load_installed_skills(paths)
    slugs = {s.slug for s in installed}
    assert "hello-world" in slugs


async def test_skill_creator_refuses_overwrite_unless_flagged(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    tool = skill_creator_tool(paths=paths)
    args = {"name": "dup-skill", "description": "first"}
    await tool.execute(args)
    out = await tool.execute(args)
    assert "already exists" in out
    out = await tool.execute({**args, "overwrite": True})
    assert "skill_creator ok" in out


async def test_skill_creator_writes_optional_tool_stub(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    tool = skill_creator_tool(paths=paths)
    await tool.execute(
        {"name": "stubby", "description": "d", "write_tool_stub": True}
    )
    stub = tmp_path / "skills" / "stubby" / "stubby.py"
    assert stub.exists()
    text = stub.read_text()
    assert "def stubby_tool" in text
    assert "FunctionTool" in text


async def test_skill_creator_includes_env_overrides(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    tool = skill_creator_tool(paths=paths)
    await tool.execute(
        {
            "name": "envful",
            "description": "d",
            "env_overrides": {"FOO": "$BAR", "STATIC": "1"},
        }
    )
    md = (tmp_path / "skills" / "envful" / "SKILL.md").read_text()
    assert "env_overrides:" in md
    assert "FOO:" in md and "$BAR" in md
