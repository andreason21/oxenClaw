"""Skill commands → auto-registered LLM tools."""

from __future__ import annotations

from oxenclaw.clawhub.frontmatter import SkillCommand, parse_skill_text
from oxenclaw.clawhub.skill_commands import (
    build_skill_command_tool,
    build_skill_command_tools,
)


def test_frontmatter_parses_commands_block() -> None:
    md = """---
name: weather
description: Look up the weather.
commands:
  - name: lookup
    description: "Get current weather for a city."
    template: 'echo "weather in {city}"'
    inputs:
      city:
        type: string
        required: true
---

# weather skill body
"""
    manifest, _body = parse_skill_text(md)
    assert manifest.name == "weather"
    assert len(manifest.commands) == 1
    cmd = manifest.commands[0]
    assert cmd.name == "lookup"
    assert "{city}" in cmd.template
    assert cmd.inputs["city"]["required"] is True


def test_command_with_no_commands_block_is_empty() -> None:
    md = """---
name: x
description: y
---

# body
"""
    manifest, _ = parse_skill_text(md)
    assert manifest.commands == []


async def test_built_tool_runs_template_with_quoted_args() -> None:
    cmd = SkillCommand(
        name="echo_test",
        description="Echo a value safely.",
        template='echo "hello {name}"',
        inputs={"name": {"type": "string", "required": True}},
    )
    tool = build_skill_command_tool("test", cmd)
    assert tool.name == "test.echo_test"
    out = await tool.execute({"name": "andrew"})
    assert "hello andrew" in out


async def test_built_tool_quotes_dangerous_input() -> None:
    """Argument values containing shell metacharacters MUST be
    quoted — `; rm -rf /` injection attempt should not fire."""
    cmd = SkillCommand(
        name="echo_safe",
        description="d",
        template="echo {payload}",
        inputs={"payload": {"type": "string", "required": True}},
    )
    tool = build_skill_command_tool("test", cmd)
    out = await tool.execute({"payload": "; touch /tmp/owned-by-attacker; echo done"})
    # The semicolons should be quoted, not executed.
    assert "; touch" in out  # echoed back as data
    assert not __import__("pathlib").Path("/tmp/owned-by-attacker").exists()


async def test_built_tool_supports_raw_escape_hatch() -> None:
    """`{!raw:name}` substitution is the explicit escape hatch when
    the skill author NEEDS unquoted shell text. Use sparingly."""
    cmd = SkillCommand(
        name="raw_pipe",
        description="d",
        template="echo hi | {!raw:filter}",
        inputs={"filter": {"type": "string", "required": True}},
    )
    tool = build_skill_command_tool("test", cmd)
    out = await tool.execute({"filter": "tr a-z A-Z"})
    assert "HI" in out


async def test_build_many_creates_namespaced_tools() -> None:
    cmds = [
        SkillCommand(name="a", description="d", template="echo a"),
        SkillCommand(name="b", description="d", template="echo b"),
    ]
    tools = build_skill_command_tools("myskill", cmds)
    assert [t.name for t in tools] == ["myskill.a", "myskill.b"]


def test_invalid_command_name_raises() -> None:
    import pytest

    with pytest.raises(Exception):
        SkillCommand(
            name="bad name with spaces",
            description="d",
            template="echo x",
        )
