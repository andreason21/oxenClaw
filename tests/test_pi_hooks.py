"""HookRunner: pluggable seams in the agent loop."""

from __future__ import annotations

from oxenclaw.pi.hooks import (
    BeforeAgentReplyResult,
    BeforeToolUseResult,
    HookContext,
    HookRunner,
)


async def test_before_agent_reply_short_circuits_when_handled() -> None:
    runner = HookRunner()

    async def handler(prompt, ctx):  # type: ignore[no-untyped-def]
        if "weather" in prompt:
            return BeforeAgentReplyResult(handled=True, reply_text="cron handled")
        return None

    runner.before_agent_reply.append(handler)
    out = await runner.run_before_agent_reply("weather today?", HookContext())
    assert out is not None and out.handled
    assert out.reply_text == "cron handled"


async def test_before_agent_reply_passes_through_when_unhandled() -> None:
    runner = HookRunner()

    async def handler(prompt, ctx):  # type: ignore[no-untyped-def]
        return None

    runner.before_agent_reply.append(handler)
    assert await runner.run_before_agent_reply("hi", HookContext()) is None


async def test_before_tool_use_can_abort_with_substitute() -> None:
    runner = HookRunner()

    async def deny(name, args, ctx):  # type: ignore[no-untyped-def]
        if name == "shell":
            return BeforeToolUseResult(abort=True, substitute_output="denied by policy")
        return None

    runner.before_tool_use.append(deny)
    out = await runner.run_before_tool_use("shell", {"cmd": "rm -rf /"}, HookContext())
    assert out is not None and out.abort
    assert out.substitute_output == "denied by policy"


async def test_before_tool_use_can_rewrite_args() -> None:
    runner = HookRunner()

    async def redact(name, args, ctx):  # type: ignore[no-untyped-def]
        if "password" in args:
            return BeforeToolUseResult(rewrite_args={**args, "password": "***"})
        return None

    runner.before_tool_use.append(redact)
    out = await runner.run_before_tool_use(
        "fetch", {"password": "secret123", "url": "x"}, HookContext()
    )
    assert out is not None and out.rewrite_args == {"password": "***", "url": "x"}


async def test_after_tool_use_logs_all_calls() -> None:
    runner = HookRunner()
    seen = []

    async def audit(name, args, output, is_error, ctx):  # type: ignore[no-untyped-def]
        seen.append((name, output, is_error))

    runner.after_tool_use.append(audit)
    await runner.run_after_tool_use("foo", {"x": 1}, "ok", False, HookContext())
    await runner.run_after_tool_use("bar", {"y": 2}, "err", True, HookContext())
    assert seen == [("foo", "ok", False), ("bar", "err", True)]


async def test_on_empty_reply_first_handler_wins() -> None:
    runner = HookRunner()

    async def first(ctx):  # type: ignore[no-untyped-def]
        return None

    async def second(ctx):  # type: ignore[no-untyped-def]
        return "fallback reply"

    async def third(ctx):  # type: ignore[no-untyped-def]
        return "should not run"

    runner.on_empty_reply.extend([first, second, third])
    assert await runner.run_on_empty_reply(HookContext()) == "fallback reply"


async def test_buggy_hook_does_not_crash_the_loop() -> None:
    runner = HookRunner()

    async def boom(name, args, ctx):  # type: ignore[no-untyped-def]
        raise RuntimeError("hook error")

    async def good(name, args, ctx):  # type: ignore[no-untyped-def]
        return BeforeToolUseResult(rewrite_args={"ok": True})

    runner.before_tool_use.extend([boom, good])
    # First raises, second still runs and wins.
    out = await runner.run_before_tool_use("x", {}, HookContext())
    assert out is not None and out.rewrite_args == {"ok": True}
