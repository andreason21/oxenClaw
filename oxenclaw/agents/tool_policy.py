"""Tool-policy pipeline — declarative allow/deny/redirect/rewrite chain.

Mirrors openclaw `agents/tool-policy-pipeline.ts`. Where a HookRunner
gives operators a programmatic API, ToolPolicy provides a *declarative*
config surface: list of rules consulted in order, each producing a
verdict (allow / deny / rewrite / redirect / substitute). The first
matching rule wins.

Use cases the hook system handles less ergonomically:
  - "deny `shell` for non-owner senders"
  - "redirect every `web_search` for weather queries to `weather`"
  - "rewrite `memory_save` args to add a `dreaming` tag for cron-
     triggered turns"
  - "substitute a stub response for `image_generate` in CI"

Rules can be loaded from config.yaml under `tool_policy:` and bound
into a HookRunner's `before_tool_use` chain by `policy_to_hook`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from oxenclaw.pi.hooks import BeforeToolUseResult, HookContext
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("agents.tool_policy")

Verdict = Literal["allow", "deny", "rewrite", "redirect", "substitute"]


@dataclass
class ToolPolicyRule:
    """One rule in the pipeline.

    Match conditions:
      - `tool_name`: exact tool name OR regex when starts with `re:`.
      - `arg_match`: dict of (arg_key, regex). All must match.
      - `sender_in`: list of sender ids (matches `ctx.session_key`
        prefix or full match). None = any sender.
      - `predicate`: optional async-friendly callable for complex matches.

    Action:
      - `verdict="allow"` — explicitly allow (no-op pass-through, used
        when a later catch-all denies and an earlier rule wants to
        whitelist).
      - `verdict="deny"` — abort with `deny_message` as substitute output.
      - `verdict="redirect"` — substitute with `redirect_to` tool name
        + carry over current args (caller must re-dispatch). NOT yet
        wired into the run loop; recorded in rule output for callers.
      - `verdict="rewrite"` — replace args with `rewrite_args`.
      - `verdict="substitute"` — abort but feed `substitute_output`
        back as the "tool result" the model sees.
    """

    tool_name: str
    verdict: Verdict
    arg_match: dict[str, str] = field(default_factory=dict)
    sender_in: list[str] | None = None
    predicate: Callable[[str, dict[str, Any], HookContext], bool] | None = None
    deny_message: str = "tool blocked by policy"
    redirect_to: str | None = None
    rewrite_args: dict[str, Any] | None = None
    substitute_output: str | None = None
    name: str = ""  # operator label for log lines

    def matches(self, tool_name: str, args: dict[str, Any], ctx: HookContext) -> bool:
        if not self._tool_matches(tool_name):
            return False
        if self.arg_match:
            for key, pattern in self.arg_match.items():
                value = args.get(key)
                if value is None:
                    return False
                try:
                    if not re.search(pattern, str(value)):
                        return False
                except re.error:
                    if pattern not in str(value):
                        return False
        if self.sender_in is not None:
            sender = ctx.session_key or ""
            if not any(s in sender for s in self.sender_in):
                return False
        if self.predicate is not None:
            try:
                if not self.predicate(tool_name, args, ctx):
                    return False
            except Exception:
                logger.exception("tool-policy predicate raised — treating as no-match")
                return False
        return True

    def _tool_matches(self, tool_name: str) -> bool:
        if self.tool_name.startswith("re:"):
            try:
                return re.fullmatch(self.tool_name[3:], tool_name) is not None
            except re.error:
                return False
        return self.tool_name == tool_name


@dataclass
class ToolPolicy:
    """Ordered chain of rules. First match wins."""

    rules: list[ToolPolicyRule] = field(default_factory=list)

    def evaluate(
        self, tool_name: str, args: dict[str, Any], ctx: HookContext
    ) -> ToolPolicyRule | None:
        for rule in self.rules:
            if rule.matches(tool_name, args, ctx):
                return rule
        return None


def policy_to_hook(
    policy: ToolPolicy,
) -> Callable[[str, dict[str, Any], HookContext], Any]:
    """Wrap a `ToolPolicy` as an async `before_tool_use` hook function.

    Behaviour:
      - `allow` → return None (let other hooks decide; pass-through).
      - `deny` / `substitute` → BeforeToolUseResult(abort=True, ...).
      - `rewrite` → BeforeToolUseResult(rewrite_args=...).
      - `redirect` → log + abort with substitute_output recommending
        the canonical tool name (the model retries on its own; we
        don't have a synchronous redispatch path here).
    """

    async def _hook(
        tool_name: str, args: dict[str, Any], ctx: HookContext
    ) -> BeforeToolUseResult | None:
        rule = policy.evaluate(tool_name, args, ctx)
        if rule is None or rule.verdict == "allow":
            return None
        label = rule.name or rule.tool_name
        if rule.verdict in ("deny", "substitute"):
            msg = rule.substitute_output if rule.verdict == "substitute" else rule.deny_message
            logger.info(
                "tool-policy %s tool=%s rule=%s",
                rule.verdict,
                tool_name,
                label,
            )
            return BeforeToolUseResult(abort=True, substitute_output=msg)
        if rule.verdict == "rewrite":
            logger.info(
                "tool-policy rewrite tool=%s rule=%s",
                tool_name,
                label,
            )
            return BeforeToolUseResult(rewrite_args=rule.rewrite_args or {})
        if rule.verdict == "redirect":
            target = rule.redirect_to or "<unknown>"
            logger.info(
                "tool-policy redirect tool=%s → %s rule=%s",
                tool_name,
                target,
                label,
            )
            return BeforeToolUseResult(
                abort=True,
                substitute_output=(
                    f"This tool was redirected by policy: call "
                    f"`{target}` instead with the same args. "
                    f"(rule: {label})"
                ),
            )
        return None

    return _hook


def policy_from_config(raw: list[dict[str, Any]] | None) -> ToolPolicy:
    """Build a `ToolPolicy` from a YAML-shaped list of dicts.

    Expected schema per rule:
        - tool: str (or `re:pattern`)
          verdict: one of allow|deny|rewrite|redirect|substitute
          name: str (label, optional)
          arg_match: { key: regex }   (optional)
          sender_in: [str]             (optional)
          deny_message: str            (verdict=deny)
          redirect_to: str             (verdict=redirect)
          rewrite_args: { ... }        (verdict=rewrite)
          substitute_output: str       (verdict=substitute)
    """
    rules: list[ToolPolicyRule] = []
    if not raw:
        return ToolPolicy()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            rules.append(
                ToolPolicyRule(
                    tool_name=str(entry["tool"]),
                    verdict=entry.get("verdict", "deny"),
                    arg_match=dict(entry.get("arg_match") or {}),
                    sender_in=entry.get("sender_in"),
                    deny_message=entry.get("deny_message", "tool blocked by policy"),
                    redirect_to=entry.get("redirect_to"),
                    rewrite_args=entry.get("rewrite_args"),
                    substitute_output=entry.get("substitute_output"),
                    name=entry.get("name", ""),
                )
            )
        except Exception:
            logger.exception("invalid tool-policy entry: %r", entry)
    return ToolPolicy(rules=rules)


__all__ = [
    "ToolPolicy",
    "ToolPolicyRule",
    "Verdict",
    "policy_from_config",
    "policy_to_hook",
]
