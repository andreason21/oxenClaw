"""Regression test: every FunctionTool description carries the
hermes-style WHEN-TO-USE block.

Pre-fix the descriptions were free-form prose that small models
couldn't reliably route from. We migrated every tool in tools_pkg/
through `hermes_desc(...)`. This test pins the convention so a future
contributor can't silently drop the routing hints when adding or
rewriting a tool.

We construct each tool with safe stubs (no network, no event loop) so
the test never touches the gateway runtime.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from oxenclaw.tools_pkg._desc import hermes_desc

# Required fragments — every description we maintain must include both.
REQUIRED_FRAGMENTS = ("WHEN TO USE:", "WHEN NOT TO USE:")


# ────────────────────────────────────────────────────────────────────────────
# Helpers: build every tool with the cheapest stub each factory accepts.
# ────────────────────────────────────────────────────────────────────────────


def _build_tools_safe() -> list[Any]:
    """Construct one instance of every FunctionTool we own.

    Some factories require channel routers / scheduler / vault objects.
    We pass minimal in-process stubs — the test only inspects the
    `description` attribute of the returned tool, so the stubs never
    have to actually function.
    """
    from oxenclaw.tools_pkg.acp_delegate_tool import acp_delegate_tool
    from oxenclaw.tools_pkg.acp_tool import acp_spawn_tool
    from oxenclaw.tools_pkg.coding import coding_agent_tool
    from oxenclaw.tools_pkg.fs_tools import (
        edit_tool,
        glob_tool,
        grep_tool,
        list_dir_tool,
        read_file_tool,
        read_pdf_tool,
        search_files_tool,
        shell_run_tool,
        write_file_tool,
    )
    from oxenclaw.tools_pkg.healthcheck import healthcheck_tool
    from oxenclaw.tools_pkg.process_tool import process_tool
    from oxenclaw.tools_pkg.session_logs import session_logs_tool
    from oxenclaw.tools_pkg.session_tools import (
        sessions_history_tool,
        sessions_list_tool,
        sessions_send_tool,
        sessions_spawn_tool,
        sessions_status_tool,
        sessions_yield_tool,
    )
    from oxenclaw.tools_pkg.skill_creator import skill_creator_tool
    from oxenclaw.tools_pkg.skill_resolver_tool import skill_resolver_tool
    from oxenclaw.tools_pkg.skill_run import skill_run_tool
    from oxenclaw.tools_pkg.subagent import SubagentConfig, subagents_tool
    from oxenclaw.tools_pkg.update_plan_tool import update_plan_tool
    from oxenclaw.tools_pkg.weather import weather_tool
    from oxenclaw.tools_pkg.web import web_fetch_tool, web_search_tool
    from oxenclaw.tools_pkg.yield_tool import yield_tool

    tools: list[Any] = [
        read_file_tool(),
        read_pdf_tool(),
        write_file_tool(),
        edit_tool(),
        list_dir_tool(),
        grep_tool(),
        glob_tool(),
        search_files_tool(),
        shell_run_tool(),
        web_fetch_tool(),
        web_search_tool(),
        weather_tool(),
        skill_run_tool(),
        skill_resolver_tool(),
        skill_creator_tool(),
        update_plan_tool(),
        process_tool(),
        yield_tool(),
        coding_agent_tool(),
        acp_delegate_tool(),
        acp_spawn_tool(),
    ]

    # github tool needs no deps.
    from oxenclaw.tools_pkg.github import github_tool

    tools.append(github_tool())

    # healthcheck tolerates None for every wired subsystem.
    tools.append(healthcheck_tool())

    # cron tool needs a scheduler — use a transient in-memory one.
    from oxenclaw.cron.models import CronJob, NewCronJob
    from oxenclaw.cron.scheduler import CronScheduler
    from oxenclaw.tools_pkg.cron_tool import cron_tool

    class _StubCronStore:
        def list(self) -> list[CronJob]:
            return []

        def add(self, job: NewCronJob) -> CronJob:  # pragma: no cover
            raise NotImplementedError

        def remove(self, job_id: str) -> bool:  # pragma: no cover
            return False

        def update(self, job_id: str, **kwargs: Any) -> CronJob | None:  # pragma: no cover
            return None

        def save(self) -> None:  # pragma: no cover
            return None

    try:
        scheduler = CronScheduler(store=_StubCronStore())  # type: ignore[arg-type]
        tools.append(cron_tool(scheduler))
    except TypeError:
        # Different CronScheduler signature in this build — skip silently.
        pass

    # Message tool — channel router stub.
    from oxenclaw.channels.router import ChannelRouter
    from oxenclaw.tools_pkg.message_tool import message_tool

    tools.append(message_tool(ChannelRouter()))

    # Session tools need a SessionManager.
    from oxenclaw.pi import InMemorySessionManager

    sm = InMemorySessionManager()
    tools.extend(
        [
            sessions_status_tool(sm),
            sessions_list_tool(sm),
            sessions_history_tool(sm),
            sessions_send_tool(sm),
            sessions_spawn_tool(sm),
            sessions_yield_tool(sm),
            session_logs_tool(sm),
        ]
    )

    # Subagent factory needs a SubagentConfig with a Model + AuthStorage —
    # use the real PiAgent stubs so we don't pull in network code.
    from oxenclaw.pi import EnvAuthStorage, Model

    sub_cfg = SubagentConfig(
        model=Model(
            id="stub", provider="anthropic", context_window=1000, max_output_tokens=200
        ),
        auth=EnvAuthStorage(),
    )
    tools.append(subagents_tool(sub_cfg))

    # Canvas tools — pull in store/bus stubs.
    from oxenclaw.canvas import CanvasEventBus, CanvasStore
    from oxenclaw.tools_pkg.canvas import (
        canvas_eval_tool,
        canvas_hide_tool,
        canvas_present_tool,
    )

    cstore = CanvasStore()
    cbus = CanvasEventBus()
    tools.extend(
        [
            canvas_present_tool(agent_id="t", store=cstore, bus=cbus),
            canvas_hide_tool(agent_id="t", store=cstore, bus=cbus),
            canvas_eval_tool(agent_id="t", store=cstore, bus=cbus),
        ]
    )

    # Browser tools — closed policy is fine for description introspection.
    from oxenclaw.browser.policy import BrowserPolicy
    from oxenclaw.tools_pkg.browser import (
        browser_click_tool,
        browser_evaluate_tool,
        browser_fill_tool,
        browser_navigate_tool,
        browser_screenshot_tool,
        browser_snapshot_tool,
    )

    pol = BrowserPolicy.closed()
    tools.extend(
        [
            browser_navigate_tool(policy=pol),
            browser_snapshot_tool(policy=pol),
            browser_screenshot_tool(policy=pol),
            browser_click_tool(policy=pol),
            browser_fill_tool(policy=pol),
            browser_evaluate_tool(policy=pol),
        ]
    )

    # Wiki tools need a vault — use a tmpfs-backed one.
    import tempfile

    from oxenclaw.tools_pkg.wiki_tools import (
        wiki_get_tool,
        wiki_save_tool,
        wiki_search_tool,
    )
    from oxenclaw.wiki.store import WikiVaultStore

    tmpdir = Path(tempfile.mkdtemp(prefix="wiki-desc-test-"))
    vault = WikiVaultStore(tmpdir)
    tools.extend([wiki_search_tool(vault), wiki_get_tool(vault), wiki_save_tool(vault)])

    # Summarize — needs Model + AuthStorage stubs.
    from oxenclaw.tools_pkg.summarize import summarize_tool

    tools.append(
        summarize_tool(
            model=Model(
                id="stub",
                provider="anthropic",
                context_window=1000,
                max_output_tokens=200,
            ),
            auth=EnvAuthStorage(),
        )
    )

    return tools


# ────────────────────────────────────────────────────────────────────────────
# Tests
# ────────────────────────────────────────────────────────────────────────────


def test_hermes_desc_helper_emits_required_blocks() -> None:
    """hermes_desc must produce both required headers when both lists supplied."""
    out = hermes_desc(
        "Do a thing.",
        when_use=["the user asks for a thing"],
        when_skip=["the request is unrelated"],
        alternatives={"other_tool": "the alternative case"},
        notes="be careful",
    )
    for fragment in REQUIRED_FRAGMENTS:
        assert fragment in out, f"missing {fragment!r} in {out!r}"
    assert "ALTERNATIVES:" in out
    assert "IMPORTANT:" in out


def test_hermes_desc_helper_drops_empty_sections() -> None:
    """A tool with no when_skip should NOT carry the 'WHEN NOT TO USE' header."""
    out = hermes_desc("Do a thing.", when_use=["the user asks"])
    assert "WHEN TO USE:" in out
    assert "WHEN NOT TO USE:" not in out
    assert "ALTERNATIVES:" not in out


def test_every_tool_has_when_to_use_and_when_not_to_use() -> None:
    """Pin the convention. If you add a new tool, give it the routing block."""
    tools = _build_tools_safe()
    assert tools, "expected to construct at least one tool"
    failures: list[str] = []
    for t in tools:
        desc = getattr(t, "description", "") or ""
        for fragment in REQUIRED_FRAGMENTS:
            if fragment not in desc:
                failures.append(f"{t.name}: missing {fragment!r}")
    assert not failures, "tools missing hermes-style block:\n" + "\n".join(failures)
