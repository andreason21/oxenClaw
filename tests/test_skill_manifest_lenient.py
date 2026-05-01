"""Skill manifest parsing must tolerate the shorthand `commands:` shape.

Real-world ClawHub skills (`stock-watcher`, etc.) ship a `commands`
list of strings like ``"/stock_alerts - Check triggered alerts"``
instead of the strict dict shape. Pre-fix, the strict pydantic schema
rejected those entries with a `ValidationError`, the whole
`skills.install` RPC failed, and `~/.oxenclaw/plugins/` ended up
empty — leaving the agent with zero registered tools. This test
locks the lenient behavior: the manifest loads, dict entries
register as runnable tools, shorthand strings become docs-only
catalog entries, and unparseable entries are dropped with a warning.
"""

from __future__ import annotations

from oxenclaw.clawhub.frontmatter import (
    SkillCommand,
    SkillManifest,
    parse_skill_text,
)
from oxenclaw.clawhub.skill_commands import build_skill_command_tools

_DICT_CMD = {
    "name": "weather_lookup",
    "description": "Look up current weather for a city.",
    "template": 'curl "wttr.in/{city}?format=3"',
    "inputs": {"city": {"type": "string", "required": True}},
}


def test_dict_command_still_parses() -> None:
    m = SkillManifest.model_validate({"name": "demo", "description": "d", "commands": [_DICT_CMD]})
    assert len(m.commands) == 1
    assert m.commands[0].name == "weather_lookup"
    assert m.commands[0].is_runnable is True


def test_shorthand_string_command_coerces_to_docs_only() -> None:
    """The exact failure shape from production: shorthand strings."""
    m = SkillManifest.model_validate(
        {
            "name": "stock-watcher",
            "description": "stock skill",
            "commands": [
                "/stock_alerts - Check triggered alerts",
                "/stock_hot - Find trending names",
                "/portfolio - Show portfolio summary",
            ],
        }
    )
    assert len(m.commands) == 3
    assert [c.name for c in m.commands] == [
        "stock_alerts",
        "stock_hot",
        "portfolio",
    ]
    assert all(c.template == "" for c in m.commands)
    assert all(c.is_runnable is False for c in m.commands)


def test_mixed_dict_and_string_commands() -> None:
    m = SkillManifest.model_validate(
        {
            "name": "mixed",
            "description": "mixed",
            "commands": [_DICT_CMD, "/extra - extra docs-only"],
        }
    )
    assert [c.name for c in m.commands] == ["weather_lookup", "extra"]
    assert m.commands[0].is_runnable is True
    assert m.commands[1].is_runnable is False


def test_shorthand_without_dash_just_uses_name_as_description() -> None:
    m = SkillManifest.model_validate(
        {"name": "x", "description": "d", "commands": ["/just_a_name"]}
    )
    assert len(m.commands) == 1
    assert m.commands[0].name == "just_a_name"
    assert m.commands[0].description == "just_a_name"


def test_unparseable_string_is_dropped_not_crash(caplog) -> None:
    import logging

    caplog.set_level(logging.WARNING)
    m = SkillManifest.model_validate(
        {
            "name": "x",
            "description": "d",
            "commands": [
                _DICT_CMD,
                "this is not a slash command",  # garbled
                "  ",  # blank
            ],
        }
    )
    # Only the dict entry survives; garbled lines were skipped.
    assert [c.name for c in m.commands] == ["weather_lookup"]
    assert any("dropping unparseable command entry" in r.getMessage() for r in caplog.records)


def test_build_tools_skips_docs_only_entries() -> None:
    """Docs-only entries (no template) MUST NOT register as tools —
    they would fail at execute time and confuse the model."""
    cmds = [
        SkillCommand(name="callable", description="c", template="echo hi"),
        SkillCommand(name="docs_only", description="d", template=""),
    ]
    tools = build_skill_command_tools("demo-skill", cmds)
    # Only the runnable one becomes a tool — name shape is the
    # builder's business; the contract this test locks is "exactly
    # one tool registered, and it's the runnable one".
    assert len(tools) == 1
    assert "callable" in tools[0].name
    assert "docs_only" not in tools[0].name


def test_full_skill_md_with_shorthand_block_loads() -> None:
    """End-to-end via `parse_skill_text` so the same code path the
    `skills.install` RPC takes is exercised."""
    text = (
        "---\n"
        "name: stock-watcher\n"
        "description: Stock + crypto monitoring tools.\n"
        "commands:\n"
        "  - /stock_alerts - Check triggered alerts\n"
        "  - /stock_hot - Find trending names & crypto\n"
        "  - /stock_rumors - Find earnings rumor activity\n"
        "  - /portfolio - Show portfolio summary\n"
        "  - /portfolio_add - Add asset to portfolio\n"
        "---\n"
        "\n"
        "Body text here.\n"
    )
    manifest, body = parse_skill_text(text)
    assert manifest.name == "stock-watcher"
    assert [c.name for c in manifest.commands] == [
        "stock_alerts",
        "stock_hot",
        "stock_rumors",
        "portfolio",
        "portfolio_add",
    ]
    assert "Body text here." in body
