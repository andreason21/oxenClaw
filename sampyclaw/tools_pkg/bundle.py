"""Curated default tool set for `sampyclaw gateway start`.

Two tiers:

- `default_bundled_tools()` — dependency-free tools. Always safe to
  register. Covers the user-facing skills the bundled SKILL.md files
  document (weather, github read-only, web_fetch, web_search,
  skill_creator) plus the core LLM helpers (summarize is excluded here
  because it needs a Model+Auth pair — wire from the agent factory).
- `bundled_tools_with_deps(...)` — accepts optional dependencies
  (ChannelRouter, CronScheduler, SessionManager, MemoryStore, …) and
  returns the dep-bound tools (healthcheck, session_logs, message,
  cron). Skip any whose dep is missing.

Result lists are flat — feed them to `ToolRegistry.register_all`.
"""

from __future__ import annotations

from typing import Any

from sampyclaw.agents.tools import Tool


def default_bundled_tools() -> list[Tool]:
    """Tools that need no runtime dependencies.

    Registered into every gateway-launched agent so the model has
    something useful to call from turn one — matches the openclaw
    out-of-the-box experience where weather / web / github work
    without any extra config.
    """
    from sampyclaw.tools_pkg.github import github_tool
    from sampyclaw.tools_pkg.skill_creator import skill_creator_tool
    from sampyclaw.tools_pkg.weather import weather_tool
    from sampyclaw.tools_pkg.web import web_fetch_tool, web_search_tool

    tools: list[Tool] = [
        weather_tool(),
        web_fetch_tool(),
        web_search_tool(),
        github_tool(),
        skill_creator_tool(),
    ]
    return tools


def bundled_tools_with_deps(
    *,
    channel_router: Any | None = None,
    cron_scheduler: Any | None = None,
    sessions: Any | None = None,
    memory: Any | None = None,
) -> list[Tool]:
    """Tools that need runtime handles. Each is added only if its
    dependency is present, so callers can pass a partial set without
    branching."""
    from sampyclaw.tools_pkg.cron_tool import cron_tool
    from sampyclaw.tools_pkg.healthcheck import healthcheck_tool
    from sampyclaw.tools_pkg.message_tool import message_tool
    from sampyclaw.tools_pkg.session_logs import session_logs_tool

    tools: list[Tool] = []
    if channel_router is not None:
        tools.append(message_tool(channel_router))
    if cron_scheduler is not None:
        tools.append(cron_tool(cron_scheduler))
    if sessions is not None:
        tools.append(session_logs_tool(sessions))
    # healthcheck takes everything optionally — register it so users can
    # introspect even partial deployments.
    tools.append(
        healthcheck_tool(
            channels=channel_router,
            cron=cron_scheduler,
            sessions=sessions,
            memory=memory,
        )
    )
    return tools


__all__ = ["bundled_tools_with_deps", "default_bundled_tools"]
