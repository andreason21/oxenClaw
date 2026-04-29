"""Curated default tool set for `oxenclaw gateway start`.

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

from oxenclaw.agents.tools import Tool


def default_bundled_tools() -> list[Tool]:
    """Tools that need no runtime dependencies.

    Registered into every gateway-launched agent so the model has
    something useful to call from turn one — matches the openclaw
    out-of-the-box experience where weather / web / github work
    without any extra config.
    """
    from oxenclaw.agents.builtin_tools import get_time_tool
    from oxenclaw.tools_pkg.acp_delegate_tool import acp_delegate_tool
    from oxenclaw.tools_pkg.acp_tool import acp_spawn_tool
    from oxenclaw.tools_pkg.github import github_tool
    from oxenclaw.tools_pkg.skill_creator import skill_creator_tool
    from oxenclaw.tools_pkg.skill_run import skill_run_tool
    from oxenclaw.tools_pkg.weather import _wttr, weather_tool
    from oxenclaw.tools_pkg.web import web_fetch_tool, web_search_tool

    # Auto-redirect handlers for `web_search` 0-hit responses. Small
    # local models (gemma4, qwen2.5:3b) routinely emit `web_search` for
    # weather/time queries and then IGNORE any text-form recovery hint.
    # By wiring the specialised tool's logic here, web_search returns
    # what looks like a successful answer on the first call.
    weather_keywords = (
        "날씨",
        "weather",
        "기온",
        "temperature",
        "forecast",
        "비 와",
        "rain",
        "snow",
        "눈 와",
    )

    async def _weather_redirect(query: str) -> str:
        # Try wttr.in with the raw query first (it accepts arbitrary
        # location strings + parses common phrasings). If that fails,
        # try common defaults — we don't have access to user-location
        # memory from here, so the caller's wider context (active-memory
        # prelude on the model side) handles location injection.
        text = await _wttr(query, "metric")
        if text:
            return text
        # Strip the "weather"/"날씨" keyword and try again with bare city.
        cleaned = query
        for kw in ("의 날씨", "날씨", "weather in", "weather", "기온", "temperature"):
            cleaned = cleaned.replace(kw, "").strip()
        if cleaned and cleaned != query:
            text = await _wttr(cleaned, "metric")
            if text:
                return text
        return ""

    time_tool = get_time_tool()

    async def _time_redirect(_query: str) -> str:
        return await time_tool.execute({})

    web_redirect_handlers: list = [
        (
            "weather",
            lambda q: any(k in q for k in weather_keywords),
            _weather_redirect,
        ),
        (
            "get_time",
            lambda q: any(
                k in q
                for k in (
                    "today",
                    "오늘",
                    "지금",
                    "current time",
                    "what time",
                    "시간",
                    "현재 시각",
                )
            ),
            _time_redirect,
        ),
    ]

    tools: list[Tool] = [
        # weather still registered as a dedicated tool — auto-redirect
        # is the safety net, not the primary path.
        weather_tool(),
        web_fetch_tool(),
        web_search_tool(redirect_handlers=web_redirect_handlers),
        github_tool(),
        skill_creator_tool(),
        # Without skill_run the model can SEE installed skills in the
        # <available_skills> block but has no way to actually execute
        # the documented scripts (no shell tool in the default
        # bundle). skill_run is the missing executor — picks an
        # interpreter from the script extension, runs in the skill's
        # cwd, and returns truncated stdout/stderr.
        skill_run_tool(),
        acp_spawn_tool(),
        # Primary ACP value: PiAgent can hand a hard sub-task to a
        # frontier ACP server (claude/codex/gemini) when the local
        # model is the wrong tool. Secondary to the above one-shot
        # `sessions_spawn`, which stays for backwards-compat.
        acp_delegate_tool(),
    ]
    return tools


def bundled_tools_with_deps(
    *,
    channel_router: Any | None = None,
    cron_scheduler: Any | None = None,
    sessions: Any | None = None,
    memory: Any | None = None,
    cron_defaults: dict[str, str] | None = None,
) -> list[Tool]:
    """Tools that need runtime handles. Each is added only if its
    dependency is present, so callers can pass a partial set without
    branching.

    `cron_defaults` (when provided) seeds the cron tool's
    `default_agent_id` / `default_channel` / `default_account_id` /
    `default_chat_id`. Without them the LLM-driven `cron(action="add")`
    path always fails with "no defaults configured" because the model
    has no way to know which channel/account/chat to wire the job to.
    Operators set them via `gateway_cmd` from env vars.
    """
    from oxenclaw.tools_pkg.cron_tool import cron_tool
    from oxenclaw.tools_pkg.healthcheck import healthcheck_tool
    from oxenclaw.tools_pkg.message_tool import message_tool
    from oxenclaw.tools_pkg.session_logs import session_logs_tool

    tools: list[Tool] = []
    if channel_router is not None:
        tools.append(message_tool(channel_router))
    if cron_scheduler is not None:
        cd = cron_defaults or {}
        tools.append(
            cron_tool(
                cron_scheduler,
                default_agent_id=cd.get("agent_id"),
                default_channel=cd.get("channel"),
                default_account_id=cd.get("account_id"),
                default_chat_id=cd.get("chat_id"),
            )
        )
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
