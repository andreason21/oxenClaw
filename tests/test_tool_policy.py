"""Tool-policy pipeline."""

from __future__ import annotations

from oxenclaw.agents.tool_policy import (
    ToolPolicy,
    ToolPolicyRule,
    policy_from_config,
    policy_to_hook,
)
from oxenclaw.pi.hooks import HookContext


def test_exact_match_deny() -> None:
    pol = ToolPolicy(
        rules=[
            ToolPolicyRule(tool_name="shell", verdict="deny", deny_message="no shell here"),
        ]
    )
    rule = pol.evaluate("shell", {"cmd": "ls"}, HookContext())
    assert rule is not None
    assert rule.verdict == "deny"


def test_regex_match_redirect() -> None:
    pol = ToolPolicy(
        rules=[
            ToolPolicyRule(
                tool_name="re:web_(search|fetch)",
                verdict="redirect",
                redirect_to="weather",
            ),
        ]
    )
    assert pol.evaluate("web_search", {}, HookContext()) is not None
    assert pol.evaluate("web_fetch", {}, HookContext()) is not None
    assert pol.evaluate("weather", {}, HookContext()) is None


def test_arg_match_filters_rule() -> None:
    pol = ToolPolicy(
        rules=[
            ToolPolicyRule(
                tool_name="memory_save",
                verdict="rewrite",
                arg_match={"text": r"password"},
                rewrite_args={"text": "[REDACTED]"},
            ),
        ]
    )
    rule = pol.evaluate("memory_save", {"text": "my password is secret"}, HookContext())
    assert rule is not None
    assert rule.rewrite_args == {"text": "[REDACTED]"}
    assert pol.evaluate("memory_save", {"text": "harmless fact"}, HookContext()) is None


def test_first_match_wins() -> None:
    pol = ToolPolicy(
        rules=[
            ToolPolicyRule(tool_name="x", verdict="allow", name="explicit-allow"),
            ToolPolicyRule(tool_name="x", verdict="deny", name="catch-all"),
        ]
    )
    rule = pol.evaluate("x", {}, HookContext())
    assert rule is not None and rule.name == "explicit-allow"


async def test_policy_to_hook_aborts_on_deny() -> None:
    pol = ToolPolicy(
        rules=[
            ToolPolicyRule(tool_name="shell", verdict="deny", deny_message="nope"),
        ]
    )
    hook = policy_to_hook(pol)
    out = await hook("shell", {}, HookContext())
    assert out is not None
    assert out.abort is True
    assert out.substitute_output == "nope"


async def test_policy_to_hook_returns_none_on_allow() -> None:
    pol = ToolPolicy(
        rules=[
            ToolPolicyRule(tool_name="x", verdict="allow"),
        ]
    )
    hook = policy_to_hook(pol)
    out = await hook("x", {}, HookContext())
    assert out is None  # let the run loop proceed


async def test_policy_to_hook_rewrites_args() -> None:
    pol = ToolPolicy(
        rules=[
            ToolPolicyRule(
                tool_name="x",
                verdict="rewrite",
                rewrite_args={"safe": True},
            ),
        ]
    )
    hook = policy_to_hook(pol)
    out = await hook("x", {}, HookContext())
    assert out is not None and out.rewrite_args == {"safe": True}


def test_policy_from_config_round_trip() -> None:
    raw = [
        {"tool": "shell", "verdict": "deny", "deny_message": "nope", "name": "no-shell"},
        {
            "tool": "re:web_.*",
            "verdict": "redirect",
            "redirect_to": "weather",
            "name": "web-to-weather",
        },
    ]
    pol = policy_from_config(raw)
    assert len(pol.rules) == 2
    assert pol.rules[0].name == "no-shell"
    assert pol.rules[1].redirect_to == "weather"


def test_policy_from_config_skips_invalid_entries() -> None:
    raw = [
        {"tool": "shell", "verdict": "deny"},
        "not a dict",  # type: ignore[list-item]
        {"missing_tool_key": "x"},
    ]
    pol = policy_from_config(raw)  # type: ignore[arg-type]
    assert len(pol.rules) == 1


def test_predicate_can_filter_dynamically() -> None:
    pol = ToolPolicy(
        rules=[
            ToolPolicyRule(
                tool_name="x",
                verdict="deny",
                predicate=lambda name, args, ctx: args.get("trigger") == "block",
            ),
        ]
    )
    assert pol.evaluate("x", {"trigger": "block"}, HookContext()) is not None
    assert pol.evaluate("x", {"trigger": "ok"}, HookContext()) is None
