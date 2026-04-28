"""Pluggable hook runner for the agent loop.

Hooks let operators inject behaviour at six well-defined seams without
modifying PiAgent or the run loop:

  - `before_agent_reply`    — before the model is even resolved. Can short-
                              circuit the whole turn (e.g. cron handler
                              that already knows the answer).
  - `before_model_resolve`  — picks the (provider, model_id) for the turn.
                              Lets a hook swap to a smaller model on
                              certain triggers.
  - `before_tool_use`       — runs before each tool call. Can rewrite args,
                              abort the call, or substitute a result.
  - `after_tool_use`        — runs after each tool call with the result.
                              Useful for audit logging and redaction.
  - `on_empty_reply`        — runs when the model returns an empty reply
                              (post stop-reason-recovery). Last chance to
                              produce a placeholder.
  - `on_turn_end`           — runs once per turn when the assistant has
                              produced its final message.

Mirrors openclaw `getGlobalHookRunner` + `runBeforeAgentReply` API in
spirit. The TS version dispatches hooks via plugins; ours uses simple
async callables registered at construction time.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("pi.hooks")


# ─── Hook context payloads ───────────────────────────────────────────


@dataclass(frozen=True)
class HookContext:
    """Identity passed to every hook so it can decide whether to fire."""

    run_id: str | None = None
    agent_id: str | None = None
    session_id: str | None = None
    session_key: str | None = None
    workspace_dir: str | None = None
    model_provider: str | None = None
    model_id: str | None = None
    trigger: str | None = None
    channel: str | None = None


@dataclass
class BeforeAgentReplyResult:
    """Return value from a `before_agent_reply` hook."""

    handled: bool = False
    reply_text: str | None = None
    # When `handled=True` the run loop short-circuits and emits the
    # supplied reply directly without calling the model.


@dataclass
class BeforeModelResolveResult:
    """Override the (provider, model_id) the run loop will resolve."""

    provider: str | None = None
    model_id: str | None = None


@dataclass
class BeforeToolUseResult:
    """Decide what happens when a tool is about to run."""

    # When `abort=True`, the tool isn't executed; the supplied
    # `substitute_output` is fed back as the tool result instead.
    abort: bool = False
    substitute_output: str | None = None
    # Optional argument rewrite. None → keep the model's args verbatim.
    rewrite_args: dict[str, Any] | None = None


# ─── Hook runner ─────────────────────────────────────────────────────


BeforeAgentReplyHook = Callable[[str, HookContext], Awaitable[BeforeAgentReplyResult | None]]
BeforeModelResolveHook = Callable[[str, HookContext], Awaitable[BeforeModelResolveResult | None]]
BeforeToolUseHook = Callable[
    [str, dict[str, Any], HookContext], Awaitable[BeforeToolUseResult | None]
]
AfterToolUseHook = Callable[[str, dict[str, Any], str, bool, HookContext], Awaitable[None]]
OnEmptyReplyHook = Callable[[HookContext], Awaitable[str | None]]
OnTurnEndHook = Callable[[str, HookContext], Awaitable[None]]


@dataclass
class HookRunner:
    """Holds hook lists and dispatches them in registration order.

    Hooks are async — synchronous logic can wrap with `asyncio.to_thread`
    if needed. Failures are caught and logged so a buggy hook never
    crashes the run loop.
    """

    before_agent_reply: list[BeforeAgentReplyHook] = field(default_factory=list)
    before_model_resolve: list[BeforeModelResolveHook] = field(default_factory=list)
    before_tool_use: list[BeforeToolUseHook] = field(default_factory=list)
    after_tool_use: list[AfterToolUseHook] = field(default_factory=list)
    on_empty_reply: list[OnEmptyReplyHook] = field(default_factory=list)
    on_turn_end: list[OnTurnEndHook] = field(default_factory=list)

    def has(self, kind: str) -> bool:
        return bool(getattr(self, kind, None))

    async def run_before_agent_reply(
        self, prompt: str, ctx: HookContext
    ) -> BeforeAgentReplyResult | None:
        for h in self.before_agent_reply:
            try:
                out = await h(prompt, ctx)
            except Exception:
                logger.exception("before_agent_reply hook raised")
                continue
            if out is not None and out.handled:
                return out
        return None

    async def run_before_model_resolve(
        self, prompt: str, ctx: HookContext
    ) -> BeforeModelResolveResult | None:
        last: BeforeModelResolveResult | None = None
        for h in self.before_model_resolve:
            try:
                out = await h(prompt, ctx)
            except Exception:
                logger.exception("before_model_resolve hook raised")
                continue
            if out is not None:
                last = out
        return last

    async def run_before_tool_use(
        self,
        tool_name: str,
        args: dict[str, Any],
        ctx: HookContext,
    ) -> BeforeToolUseResult | None:
        for h in self.before_tool_use:
            try:
                out = await h(tool_name, args, ctx)
            except Exception:
                logger.exception("before_tool_use hook raised: tool=%s", tool_name)
                continue
            if out is not None and (out.abort or out.rewrite_args is not None):
                return out
        return None

    async def run_after_tool_use(
        self,
        tool_name: str,
        args: dict[str, Any],
        output: str,
        is_error: bool,
        ctx: HookContext,
    ) -> None:
        for h in self.after_tool_use:
            try:
                await h(tool_name, args, output, is_error, ctx)
            except Exception:
                logger.exception("after_tool_use hook raised: tool=%s", tool_name)

    async def run_on_empty_reply(self, ctx: HookContext) -> str | None:
        for h in self.on_empty_reply:
            try:
                out = await h(ctx)
            except Exception:
                logger.exception("on_empty_reply hook raised")
                continue
            if out:
                return out
        return None

    async def run_on_turn_end(self, final_text: str, ctx: HookContext) -> None:
        for h in self.on_turn_end:
            try:
                await h(final_text, ctx)
            except Exception:
                logger.exception("on_turn_end hook raised")


__all__ = [
    "AfterToolUseHook",
    "BeforeAgentReplyHook",
    "BeforeAgentReplyResult",
    "BeforeModelResolveHook",
    "BeforeModelResolveResult",
    "BeforeToolUseHook",
    "BeforeToolUseResult",
    "HookContext",
    "HookRunner",
    "OnEmptyReplyHook",
    "OnTurnEndHook",
]
