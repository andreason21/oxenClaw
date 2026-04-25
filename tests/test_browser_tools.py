"""Smoke tests for sampyclaw.tools_pkg.browser tool factories.

Live browser interaction is not exercised here — the heavy E2E tests
require `playwright install chromium` and a network. We instead verify
the factory plumbing: tools register, schemas serialise, evaluate +
download tools refuse to instantiate against the closed default policy.
"""

from __future__ import annotations

import pytest

from sampyclaw.agents.tools import ToolRegistry
from sampyclaw.browser.errors import BrowserPolicyError
from sampyclaw.browser.policy import BrowserPolicy
from sampyclaw.security.net.policy import NetPolicy
from sampyclaw.tools_pkg.browser import (
    browser_download_tool,
    browser_evaluate_tool,
    browser_navigate_tool,
    browser_screenshot_tool,
    browser_snapshot_tool,
    default_browser_tools,
)


def _policy_open() -> BrowserPolicy:
    return BrowserPolicy(net=NetPolicy(allowed_hostnames=("example.com",)))


def test_default_browser_tools_registers_five() -> None:
    reg = ToolRegistry()
    reg.register_all(default_browser_tools(policy=_policy_open()))
    expected = {
        "browser_navigate",
        "browser_snapshot",
        "browser_screenshot",
        "browser_click",
        "browser_fill",
    }
    assert set(reg.names()) == expected


def test_each_tool_emits_schema() -> None:
    pol = _policy_open()
    for tool in [
        browser_navigate_tool(policy=pol),
        browser_snapshot_tool(policy=pol),
        browser_screenshot_tool(policy=pol),
        browser_evaluate_tool(policy=pol),
    ]:
        schema = tool.input_schema
        assert "url" in schema["properties"]


def test_download_tool_refuses_when_disallowed() -> None:
    pol = _policy_open()  # allow_downloads=False
    with pytest.raises(BrowserPolicyError):
        browser_download_tool(policy=pol)


def test_download_tool_constructs_when_allowed() -> None:
    pol = BrowserPolicy(
        net=NetPolicy(allowed_hostnames=("example.com",)),
        allow_downloads=True,
    )
    tool = browser_download_tool(policy=pol)
    assert tool.name == "browser_download"


def test_factory_returns_no_browser_tools_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAMPYCLAW_ENABLE_BROWSER", raising=False)
    from sampyclaw.agents.factory import _maybe_browser_tools

    assert _maybe_browser_tools() == []


def test_factory_returns_browser_tools_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAMPYCLAW_ENABLE_BROWSER", "1")
    monkeypatch.setenv("SAMPYCLAW_NET_ALLOW_HOSTS", "example.com")
    from sampyclaw.agents.factory import _maybe_browser_tools

    tools = _maybe_browser_tools()
    assert len(tools) == 5
