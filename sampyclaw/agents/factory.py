"""Construct an Agent from provider name + options.

Shared by the CLI (`gateway start --provider anthropic`) and the
`agents.create` gateway RPC, so agent construction lives in one place.
"""

from __future__ import annotations

import asyncio
import os

from sampyclaw.agents.base import Agent
from sampyclaw.agents.builtin_tools import default_tools
from sampyclaw.agents.echo import EchoAgent
from sampyclaw.agents.local_agent import VLLM_DEFAULT_BASE_URL, LocalAgent
from sampyclaw.agents.pi_agent import PiAgent
from sampyclaw.agents.tools import Tool, ToolRegistry

# When `--provider anthropic` is invoked without an explicit `--model`,
# we route through PiAgent and pick the latest mid-tier Sonnet so
# behaviour stays close to the old inline AnthropicAgent default.
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"


def _maybe_canvas_tools(agent_id: str) -> list[Tool]:
    """Append canvas tools when SAMPYCLAW_ENABLE_CANVAS is set.

    Reuses the process-wide CanvasStore + CanvasEventBus singletons so
    every agent shares the same dashboard target.
    """
    if os.environ.get("SAMPYCLAW_ENABLE_CANVAS", "").lower() not in ("1", "true", "yes"):
        return []
    try:
        from sampyclaw.canvas import (
            get_default_canvas_bus,
            get_default_canvas_store,
        )
        from sampyclaw.tools_pkg.canvas import default_canvas_tools

        return list(
            default_canvas_tools(
                agent_id=agent_id,
                store=get_default_canvas_store(),
                bus=get_default_canvas_bus(),
            )
        )
    except Exception:
        return []


def _maybe_browser_tools() -> list[Tool]:
    """Append browser tools when SAMPYCLAW_ENABLE_BROWSER is set + playwright present.

    Failures (missing optional dep) are logged once and swallowed so the
    gateway still boots. The opt-in is opt-in: no env, no tools.
    """
    if os.environ.get("SAMPYCLAW_ENABLE_BROWSER", "").lower() not in ("1", "true", "yes"):
        return []
    try:
        from sampyclaw.browser.policy import BrowserPolicy
        from sampyclaw.tools_pkg.browser import default_browser_tools
    except Exception:
        return []
    try:
        return list(default_browser_tools(policy=BrowserPolicy.from_env()))
    except Exception:
        return []


class UnknownProvider(ValueError):
    """Raised when `provider` is not one of the supported names."""


# `pi` is the pi-embedded-runner-backed agent — full streaming, tool
# loop, compaction, cache observability, multi-provider.
# `vllm` is a thin alias of `local` with strict-OpenAI payload (no Ollama
# extras) and warmup off; defaults to vLLM's canonical 127.0.0.1:8000/v1.
# `anthropic` is a thin alias of `pi` pinned to a Claude default model
# (the inline AnthropicAgent was removed in favour of PiAgent's richer
# Anthropic path: cache_control, thinking, cache observability,
# compaction, persistence).
SUPPORTED_PROVIDERS: tuple[str, ...] = ("pi", "local", "vllm", "echo", "anthropic")


def build_agent(
    *,
    agent_id: str,
    provider: str,
    system_prompt: str | None = None,
    model: str | None = None,
    tools: ToolRegistry | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    memory=None,  # type: ignore[no-untyped-def]
    mcp_tools: list[Tool] | None = None,
) -> Agent:
    """Build an agent. Provider-specific kwargs are silently ignored when not applicable."""
    if provider == "echo":
        return EchoAgent(agent_id=agent_id)
    if provider in ("pi", "anthropic"):
        resolved_tools = tools
        if resolved_tools is None:
            resolved_tools = ToolRegistry()
            resolved_tools.register_all(default_tools())
            canvas_tools = _maybe_canvas_tools(agent_id)
            if canvas_tools:
                resolved_tools.register_all(canvas_tools)
            browser_tools = _maybe_browser_tools()
            if browser_tools:
                resolved_tools.register_all(browser_tools)
        if mcp_tools:
            resolved_tools.register_all(list(mcp_tools))
        kwargs: dict = {"agent_id": agent_id, "tools": resolved_tools}  # type: ignore[type-arg]
        if system_prompt is not None:
            kwargs["system_prompt"] = system_prompt
        resolved_model = model
        if provider == "anthropic" and resolved_model is None:
            resolved_model = DEFAULT_ANTHROPIC_MODEL
        if resolved_model is not None:
            kwargs["model_id"] = resolved_model
        if memory is not None:
            kwargs["memory"] = memory
        return PiAgent(**kwargs)
    if provider in ("local", "vllm"):
        resolved_tools = tools
        if resolved_tools is None:
            resolved_tools = ToolRegistry()
            resolved_tools.register_all(default_tools())
            canvas_tools = _maybe_canvas_tools(agent_id)
            if canvas_tools:
                resolved_tools.register_all(canvas_tools)
            browser_tools = _maybe_browser_tools()
            if browser_tools:
                resolved_tools.register_all(browser_tools)
        if mcp_tools:
            resolved_tools.register_all(list(mcp_tools))
        kwargs: dict = {"agent_id": agent_id, "tools": resolved_tools}  # type: ignore[type-arg]
        if system_prompt is not None:
            kwargs["system_prompt"] = system_prompt
        if model is not None:
            kwargs["model"] = model
        if memory is not None:
            kwargs["memory"] = memory
        if provider == "vllm":
            kwargs["flavor"] = "vllm"
            kwargs["base_url"] = base_url if base_url is not None else VLLM_DEFAULT_BASE_URL
        elif base_url is not None:
            kwargs["base_url"] = base_url
        if api_key is not None:
            kwargs["api_key"] = api_key
        return LocalAgent(**kwargs)
    raise UnknownProvider(
        f"unknown agent provider: {provider!r} (supported: {', '.join(SUPPORTED_PROVIDERS)})"
    )


async def load_mcp_tools(
    paths=None,  # type: ignore[no-untyped-def]
    *,
    reserved_names: list[str] | tuple[str, ...] | None = None,
) -> tuple[list[Tool], object | None]:
    """Convenience: load `mcp.json`, materialize tools, return `(tools, pool)`.

    `pool` is `None` when no servers are configured. When non-None, callers
    are responsible for `await pool.close()` on shutdown so subprocesses /
    SSE streams clean up. Failures connecting to individual servers are
    isolated — their tools simply don't appear in the returned list and
    the failure reason is logged + accessible via `pool.failures`.
    """
    from sampyclaw.pi.mcp import build_pool_from_config, materialize_mcp_tools

    pool = build_pool_from_config(paths)
    if pool is None:
        return [], None
    tools = await materialize_mcp_tools(pool, reserved_names=reserved_names)
    return tools, pool


def load_mcp_tools_sync(
    paths=None,  # type: ignore[no-untyped-def]
    *,
    reserved_names: list[str] | tuple[str, ...] | None = None,
) -> tuple[list[Tool], object | None]:
    """Sync wrapper around `load_mcp_tools` for non-async callers (CLI).

    Must NOT be called from inside a running event loop — use the async
    variant there.
    """
    return asyncio.run(load_mcp_tools(paths, reserved_names=reserved_names))
