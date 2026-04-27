"""Construct an Agent from provider name + options.

**openclaw-style routing.** All catalog providers go through one runtime
(`PiAgent` on top of `oxenclaw.pi.run`). The CLI's `--provider` argument
selects the **catalog provider id** of the model — same role as openclaw's
`registerProviderStreamForModel({ model.provider, ... })`. Adding a new
provider is a single change in `oxenclaw/pi/providers/`.

The pre-pi shortcut classes (`LocalAgent`, `EchoAgent`) still exist in
the tree:
- `EchoAgent` is the test-only `--provider echo` route.
- `LocalAgent` is no longer wired through this factory but stays on
  disk for direct construction in legacy tests / migration code.

Legacy provider names (`local`, `pi`, `vllm`) are accepted for
back-compat with existing config.yaml files but emit a deprecation log
and are mapped to their canonical catalog id (`ollama`, `ollama`, `vllm`
respectively).
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import replace

from oxenclaw.agents.base import Agent
from oxenclaw.agents.builtin_tools import default_tools
from oxenclaw.agents.echo import EchoAgent
from oxenclaw.agents.pi_agent import PiAgent
from oxenclaw.agents.tools import Tool, ToolRegistry

# CodingAgent imports tools_pkg.update_plan_tool which (transitively, via
# `oxenclaw.agents.tools` evaluation) re-enters this module. Defer the
# import to use-site to break the cycle. The TYPE_CHECKING shim keeps
# annotations happy without triggering the actual import at import time.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oxenclaw.agents.coding_agent import CodingAgent
from oxenclaw.pi.catalog import default_registry
from oxenclaw.pi.models import Model
from oxenclaw.pi.registry import InMemoryAuthStorage
from oxenclaw.pi.session import SessionManager

logger = logging.getLogger(__name__)


# Catalog providers — every provider id that has a stream wrapper
# registered in `oxenclaw/pi/providers/`. Keep this list in sync with
# the `register_provider_stream(...)` calls there; the test
# `tests/test_agents_factory.py::test_catalog_providers_match_pi_registrations`
# enforces the invariant.
CATALOG_PROVIDERS: tuple[str, ...] = (
    "anthropic",
    "anthropic-vertex",
    "bedrock",
    "deepseek",
    "fireworks",
    "google",
    "groq",
    "kilocode",
    "litellm",
    "llamacpp",
    "lmstudio",
    "minimax",
    "mistral",
    "moonshot",
    "ollama",
    "openai",
    "openai-compatible",
    "openrouter",
    "proxy",
    "together",
    "vertex-ai",
    "vllm",
    "zai",
)

# `echo` is a hidden test backend — not a real provider, but exposed
# here so the dashboard / tests can construct one without special-casing.
SUPPORTED_PROVIDERS: tuple[str, ...] = (*CATALOG_PROVIDERS, "echo")

# Legacy aliases — accepted with a deprecation warning, mapped to the
# canonical catalog id. Mirrors openclaw's `normalizeProviderId`.
LEGACY_ALIASES: dict[str, str] = {
    "local": "ollama",  # pre-pi LocalAgent was always Ollama-shaped
    "pi": "ollama",  # pi default model was gemma4:latest (Ollama)
    "aws-bedrock": "bedrock",  # openclaw's normalizeProviderId mapping
    "z.ai": "zai",
    "z-ai": "zai",
}

# Default model when `--model` is omitted. Picked to be cheap + first-run
# friendly; users can always override with `--model <id>`.
PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    "anthropic-vertex": "claude-sonnet-4-6",
    "openai": "gpt-4o-mini",
    "google": "gemini-2.0-flash",
    "vertex-ai": "gemini-2.0-flash",
    "ollama": "gemma4:latest",
    "vllm": "gemma4:latest",
    "lmstudio": "gemma4:latest",
    "llamacpp": "gemma4:latest",
    "openai-compatible": "gemma4:latest",
    "proxy": "gemma4:latest",
    "litellm": "gemma4:latest",
}

# Reasonable default context windows for synthesised (non-catalog) models.
# Local providers usually serve OSS models with 128K windows; hosted
# providers tend to be bigger but unknown — pick a conservative 128K.
_SYNTHETIC_CONTEXT_WINDOW = 128_000
_SYNTHETIC_MAX_OUTPUT = 4_096


class UnknownProvider(ValueError):
    """Raised when `provider` is not a recognised catalog id or alias."""


def _resolve_provider(provider: str) -> str:
    """Map legacy aliases to canonical catalog ids."""
    if provider in LEGACY_ALIASES:
        canonical = LEGACY_ALIASES[provider]
        logger.warning(
            "provider %r is a legacy alias for %r; update your config to use the canonical name",
            provider,
            canonical,
        )
        return canonical
    return provider


def _maybe_canvas_tools(agent_id: str) -> list[Tool]:
    """Append canvas tools when OXENCLAW_ENABLE_CANVAS is set."""
    if os.environ.get("OXENCLAW_ENABLE_CANVAS", "").lower() not in ("1", "true", "yes"):
        return []
    try:
        from oxenclaw.canvas import (
            get_default_canvas_bus,
            get_default_canvas_store,
        )
        from oxenclaw.tools_pkg.canvas import default_canvas_tools

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
    """Append browser tools when OXENCLAW_ENABLE_BROWSER is set + playwright present."""
    if os.environ.get("OXENCLAW_ENABLE_BROWSER", "").lower() not in ("1", "true", "yes"):
        return []
    try:
        from oxenclaw.browser.policy import BrowserPolicy
        from oxenclaw.tools_pkg.browser import default_browser_tools
    except Exception:
        return []
    try:
        return list(default_browser_tools(policy=BrowserPolicy.from_env()))
    except Exception:
        return []


def _build_default_tools(
    agent_id: str,
    mcp_tools: list[Tool] | None,
    *,
    session_manager: SessionManager | None = None,
    approval_manager=None,  # type: ignore[no-untyped-def]
) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register_all(default_tools())

    # Mutating fs/shell/process/plan tools — every agent (not just
    # CodingAgent) gets these, with approval-gating when an
    # ApprovalManager is wired so the destructive ones land on the
    # exec-approvals queue instead of running unattended.
    from oxenclaw.tools_pkg.fs_tools import edit_tool, shell_run_tool, write_file_tool
    from oxenclaw.tools_pkg.process_tool import process_tool
    from oxenclaw.tools_pkg.update_plan_tool import update_plan_tool

    raw_mut = [
        write_file_tool(),
        edit_tool(),
        shell_run_tool(),
        process_tool(),
    ]
    if approval_manager is not None:
        from oxenclaw.approvals.tool_wrap import gated_tool

        for t in raw_mut:
            reg.register(gated_tool(t, approval_manager=approval_manager))
    else:
        # No approver wired — register raw. Operators who don't want
        # a default agent to write files / run shell on this box
        # should set OXENCLAW_APPROVER_TOKEN and accept the prompts.
        reg.register_all(raw_mut)

    # update_plan is ungated (writes a plan json next to session, no
    # external side-effects). Always available to give the model a
    # structured way to track multi-step work.
    reg.register(update_plan_tool())

    canvas = _maybe_canvas_tools(agent_id)
    if canvas:
        reg.register_all(canvas)
    browser = _maybe_browser_tools()
    if browser:
        reg.register_all(browser)
    if mcp_tools:
        reg.register_all(list(mcp_tools))
    if session_manager is not None:
        from oxenclaw.tools_pkg.session_tools import build_session_tools

        reg.register_all(build_session_tools(session_manager, approval_manager=approval_manager))
    return reg


def _registry_for(
    provider: str,
    model_id: str,
    base_url: str | None,
):
    """Return a ModelRegistry that has `model_id` registered with the
    requested `provider` and (optional) `base_url` override.

    - Catalog hit + matching provider + no base_url override → registry as-is.
    - Catalog hit + provider/base_url mismatch → register a `replace()`d copy.
    - Catalog miss → synthesise a transient entry with conservative defaults.
    """
    reg = default_registry()
    existing = reg.get(model_id)
    if existing is None:
        synthetic = Model(
            id=model_id,
            provider=provider,
            context_window=_SYNTHETIC_CONTEXT_WINDOW,
            max_output_tokens=_SYNTHETIC_MAX_OUTPUT,
            extra={"base_url": base_url} if base_url else {},
        )
        reg.register(synthetic)
        return reg
    needs_provider_override = existing.provider != provider
    needs_base_url_override = base_url is not None and existing.extra.get("base_url") != base_url
    if needs_provider_override or needs_base_url_override:
        new_extra = dict(existing.extra)
        if base_url is not None:
            new_extra["base_url"] = base_url
        reg.register(replace(existing, provider=provider, extra=new_extra))
    return reg


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
    agent_type: str = "pi",
    session_manager: SessionManager | None = None,
    approval_manager=None,  # type: ignore[no-untyped-def]
) -> Agent:
    """Build an agent. All catalog providers route through `PiAgent`.

    Parameters
    ----------
    agent_type:
        ``"pi"`` (default) — standard PiAgent.
        ``"coding"`` — CodingAgent subclass with curated file-system + shell
        tools and a plan-first system prompt.  The ``tools`` override is
        ignored for ``"coding"`` so the curated registry is always intact;
        pass ``approval_manager`` via a direct CodingAgent construction if
        approval gating is needed.
    """
    if provider == "echo":
        return EchoAgent(agent_id=agent_id)

    canonical = _resolve_provider(provider)
    if canonical not in CATALOG_PROVIDERS:
        raise UnknownProvider(
            f"unknown agent provider: {provider!r} (supported: {', '.join(SUPPORTED_PROVIDERS)})"
        )

    resolved_model_id = model or PROVIDER_DEFAULT_MODELS.get(canonical)
    if resolved_model_id is None:
        raise UnknownProvider(
            f"no default model registered for provider {canonical!r}; pass --model explicitly"
        )

    registry = _registry_for(canonical, resolved_model_id, base_url)
    auth = InMemoryAuthStorage({canonical: api_key}) if api_key else None

    # CodingAgent builds its own curated tool registry; the generic
    # mcp_tools / default-tools path is intentionally bypassed so the
    # coding-specific tool set is always intact.
    if agent_type == "coding":
        kwargs: dict = {  # type: ignore[type-arg]
            "agent_id": agent_id,
            "model_id": resolved_model_id,
            "registry": registry,
        }
        if auth is not None:
            kwargs["auth"] = auth
        if system_prompt is not None:
            kwargs["system_prompt"] = system_prompt
        if memory is not None:
            kwargs["memory"] = memory
        if session_manager is not None:
            kwargs["session_manager"] = session_manager
        if approval_manager is not None:
            kwargs["approval_manager"] = approval_manager
        from oxenclaw.agents.coding_agent import CodingAgent  # lazy: see top
        return CodingAgent(**kwargs)

    resolved_tools = (
        tools
        if tools is not None
        else _build_default_tools(
            agent_id,
            mcp_tools,
            session_manager=session_manager,
            approval_manager=approval_manager,
        )
    )
    if tools is not None and mcp_tools:
        resolved_tools.register_all(list(mcp_tools))

    kwargs = {  # type: ignore[assignment]
        "agent_id": agent_id,
        "model_id": resolved_model_id,
        "tools": resolved_tools,
        "registry": registry,
    }
    if auth is not None:
        kwargs["auth"] = auth
    if system_prompt is not None:
        kwargs["system_prompt"] = system_prompt
    if memory is not None:
        kwargs["memory"] = memory

    return PiAgent(**kwargs)


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
    from oxenclaw.pi.mcp import build_pool_from_config, materialize_mcp_tools

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


__all__ = [
    "CATALOG_PROVIDERS",
    "LEGACY_ALIASES",
    "PROVIDER_DEFAULT_MODELS",
    "SUPPORTED_PROVIDERS",
    "UnknownProvider",
    "build_agent",
    "load_mcp_tools",
    "load_mcp_tools_sync",
]
