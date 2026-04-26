"""LLM-callable browser tools backed by Playwright.

All tools route through `PlaywrightSession.context()` so every request
the page makes flows through `build_route_handler` (URL preflight + DNS
pinning + audit). Tool outputs are truncated via
`truncate_tool_result` so a runaway page can't blow out the model's
context window.

Exposed tools:

- `browser_navigate(url, wait_until=)` — single-shot fetch, returns the
  final URL + HTTP status + page title.
- `browser_snapshot(url, format=text|html|aria, max_chars=)` — opens
  the page and returns DOM/ARIA tree text.
- `browser_screenshot(url, full_page=, max_bytes=)` — base64 PNG.
- `browser_click(url, selector)` — navigates then clicks; returns the
  resulting URL.
- `browser_fill(url, selector, value)` — fills a form field then
  returns the page text.
- `browser_evaluate(url, expression, max_chars=)` — runs a JS
  expression in page context, returns the JSON-serialised result
  (truncated). Disabled unless `policy.allow_websockets` style flag is
  added later — for now gated by approval.
- `browser_download(url, dest_dir, max_bytes=)` — saves a download
  into the given directory (typically a skill's ephemeral workspace).
"""

from __future__ import annotations

import base64
from contextlib import suppress
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.browser.errors import (
    BrowserPolicyError,
    BrowserResourceCapError,
)
from oxenclaw.browser.policy import BrowserPolicy
from oxenclaw.browser.session import PlaywrightSession, get_default_session
from oxenclaw.pi.tool_runtime import truncate_tool_result
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("tools.browser")


# ─── arg models ────────────────────────────────────────────────────


class _NavigateArgs(BaseModel):
    model_config = {"extra": "forbid"}
    url: str = Field(..., description="Absolute URL to navigate to.")
    wait_until: str = Field(
        default="load",
        description=(
            "Playwright lifecycle event: 'load', 'domcontentloaded', 'networkidle', or 'commit'."
        ),
    )


class _SnapshotArgs(BaseModel):
    model_config = {"extra": "forbid"}
    url: str = Field(..., description="URL to snapshot.")
    format: str = Field(
        default="text",
        description="One of 'text' (visible text), 'html' (full HTML), 'aria' (ARIA tree).",
    )
    max_chars: int | None = Field(
        default=None, description="Override the default truncation cap (capped by policy)."
    )


class _ScreenshotArgs(BaseModel):
    model_config = {"extra": "forbid"}
    url: str = Field(..., description="URL to capture.")
    full_page: bool = Field(default=False, description="If true, capture the full scrollable page.")


class _ClickArgs(BaseModel):
    model_config = {"extra": "forbid"}
    url: str = Field(..., description="URL to load before clicking.")
    selector: str = Field(..., description="Playwright/CSS selector to click.")


class _FillArgs(BaseModel):
    model_config = {"extra": "forbid"}
    url: str = Field(..., description="URL hosting the form.")
    selector: str = Field(..., description="CSS selector for the input.")
    value: str = Field(..., description="Value to fill in.")


class _EvaluateArgs(BaseModel):
    model_config = {"extra": "forbid"}
    url: str = Field(..., description="URL to evaluate against.")
    expression: str = Field(..., description="JavaScript expression to evaluate in page context.")


class _DownloadArgs(BaseModel):
    model_config = {"extra": "forbid"}
    url: str = Field(..., description="URL that triggers a download.")
    dest_dir: str = Field(..., description="Directory to save the download into.")


# ─── helpers ───────────────────────────────────────────────────────


async def _open_session(
    session: PlaywrightSession | None,
    policy: BrowserPolicy,
) -> PlaywrightSession:
    if session is not None:
        return session
    return await get_default_session(policy=policy)


def _format_status(url: str, status: int | None, title: str | None) -> str:
    return f"navigated -> {url}\nstatus: {status}\ntitle: {title or ''}"


# ─── tool factories ────────────────────────────────────────────────


def browser_navigate_tool(
    *,
    policy: BrowserPolicy | None = None,
    session: PlaywrightSession | None = None,
) -> Tool:
    pol = policy or BrowserPolicy.closed()

    async def _handler(args: _NavigateArgs) -> str:
        sess = await _open_session(session, pol)
        async with sess.page(policy=pol) as page:
            response = await page.goto(args.url, wait_until=args.wait_until)
            status = response.status if response is not None else None
            title: str | None = None
            with suppress(Exception):
                title = await page.title()
            return _format_status(page.url, status, title)

    return FunctionTool(
        name="browser_navigate",
        description=(
            "Open a URL in a sandboxed headless browser and report the "
            "final URL, HTTP status and page title. The browser refuses "
            "any URL that does not pass the configured net policy."
        ),
        input_model=_NavigateArgs,
        handler=_handler,
    )


def browser_snapshot_tool(
    *,
    policy: BrowserPolicy | None = None,
    session: PlaywrightSession | None = None,
) -> Tool:
    pol = policy or BrowserPolicy.closed()

    async def _handler(args: _SnapshotArgs) -> str:
        cap = min(args.max_chars or pol.max_dom_chars, pol.max_dom_chars)
        sess = await _open_session(session, pol)
        async with sess.page(policy=pol) as page:
            await page.goto(args.url)
            if args.format == "html":
                content = await page.content()
            elif args.format == "aria":
                tree = await page.accessibility.snapshot()
                content = _render_aria(tree)
            else:
                content = await page.evaluate("document.body ? document.body.innerText : ''")
            truncated, _ = truncate_tool_result(content or "", max_chars=cap)
            return truncated

    return FunctionTool(
        name="browser_snapshot",
        description=(
            "Load a URL and return its DOM as text, raw HTML, or ARIA tree. "
            "Output is truncated to the policy's max_dom_chars."
        ),
        input_model=_SnapshotArgs,
        handler=_handler,
    )


def browser_screenshot_tool(
    *,
    policy: BrowserPolicy | None = None,
    session: PlaywrightSession | None = None,
) -> Tool:
    pol = policy or BrowserPolicy.closed()

    async def _handler(args: _ScreenshotArgs) -> str:
        sess = await _open_session(session, pol)
        async with sess.page(policy=pol) as page:
            await page.goto(args.url)
            png = await page.screenshot(full_page=args.full_page, type="png")
            if len(png) > pol.max_screenshot_bytes:
                raise BrowserResourceCapError(
                    f"screenshot {len(png)} bytes exceeds cap {pol.max_screenshot_bytes}"
                )
            b64 = base64.b64encode(png).decode("ascii")
            return f"data:image/png;base64,{b64}"

    return FunctionTool(
        name="browser_screenshot",
        description=(
            "Capture a PNG screenshot of a URL. Returns a base64 data URI. "
            "Refuses captures larger than the policy's max_screenshot_bytes."
        ),
        input_model=_ScreenshotArgs,
        handler=_handler,
    )


def browser_click_tool(
    *,
    policy: BrowserPolicy | None = None,
    session: PlaywrightSession | None = None,
) -> Tool:
    pol = policy or BrowserPolicy.closed()

    async def _handler(args: _ClickArgs) -> str:
        sess = await _open_session(session, pol)
        async with sess.page(policy=pol) as page:
            await page.goto(args.url)
            await page.click(args.selector)
            with suppress(Exception):
                await page.wait_for_load_state("domcontentloaded")
            return f"clicked {args.selector!r}; current url: {page.url}"

    return FunctionTool(
        name="browser_click",
        description="Navigate to a URL and click a selector. Returns the resulting URL.",
        input_model=_ClickArgs,
        handler=_handler,
    )


def browser_fill_tool(
    *,
    policy: BrowserPolicy | None = None,
    session: PlaywrightSession | None = None,
) -> Tool:
    pol = policy or BrowserPolicy.closed()

    async def _handler(args: _FillArgs) -> str:
        sess = await _open_session(session, pol)
        async with sess.page(policy=pol) as page:
            await page.goto(args.url)
            await page.fill(args.selector, args.value)
            return f"filled {args.selector!r} with {len(args.value)} chars"

    return FunctionTool(
        name="browser_fill",
        description="Fill a form input on a URL with the given value.",
        input_model=_FillArgs,
        handler=_handler,
    )


def browser_evaluate_tool(
    *,
    policy: BrowserPolicy | None = None,
    session: PlaywrightSession | None = None,
) -> Tool:
    pol = policy or BrowserPolicy.closed()

    async def _handler(args: _EvaluateArgs) -> str:
        sess = await _open_session(session, pol)
        async with sess.page(policy=pol) as page:
            await page.goto(args.url)
            value = await page.evaluate(args.expression)
            text = _safe_str(value)
            truncated, _ = truncate_tool_result(text, max_chars=pol.max_eval_chars)
            return truncated

    return FunctionTool(
        name="browser_evaluate",
        description=(
            "Evaluate a JavaScript expression in the page context and return "
            "its JSON-stringified result (truncated to policy max_eval_chars)."
        ),
        input_model=_EvaluateArgs,
        handler=_handler,
    )


def browser_download_tool(
    *,
    policy: BrowserPolicy | None = None,
    session: PlaywrightSession | None = None,
) -> Tool:
    pol = policy or BrowserPolicy.closed()
    if not pol.allow_downloads:
        raise BrowserPolicyError("browser_download requires BrowserPolicy.allow_downloads=True")

    async def _handler(args: _DownloadArgs) -> str:
        dest = Path(args.dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        sess = await _open_session(session, pol)
        async with sess.page(policy=pol) as page:
            async with page.expect_download() as download_info:
                await page.goto(args.url)
            download = await download_info.value
            target = dest / download.suggested_filename
            await download.save_as(target)
            size = target.stat().st_size
            if size > pol.max_download_bytes:
                target.unlink(missing_ok=True)
                raise BrowserResourceCapError(
                    f"download {size} bytes exceeds cap {pol.max_download_bytes}"
                )
            return f"saved {download.suggested_filename} ({size} bytes) -> {target}"

    return FunctionTool(
        name="browser_download",
        description=(
            "Trigger a download from a URL and save it under the given dest_dir. "
            "Refuses downloads larger than the policy's max_download_bytes."
        ),
        input_model=_DownloadArgs,
        handler=_handler,
    )


def default_browser_tools(
    *,
    policy: BrowserPolicy | None = None,
    session: PlaywrightSession | None = None,
) -> list[Tool]:
    """Bundle of always-safe browser tools (excludes downloads + evaluate).

    Operators that want richer surface explicitly add `browser_evaluate_tool`
    and `browser_download_tool` themselves once they've reviewed risk.
    """
    return [
        browser_navigate_tool(policy=policy, session=session),
        browser_snapshot_tool(policy=policy, session=session),
        browser_screenshot_tool(policy=policy, session=session),
        browser_click_tool(policy=policy, session=session),
        browser_fill_tool(policy=policy, session=session),
    ]


# ─── small helpers ─────────────────────────────────────────────────


def _safe_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        import json

        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _render_aria(node: Any, depth: int = 0) -> str:
    if not node:
        return ""
    role = node.get("role", "")
    name = node.get("name", "")
    line = "  " * depth + f"[{role}] {name}".rstrip()
    children = node.get("children") or []
    out = [line]
    for child in children:
        out.append(_render_aria(child, depth + 1))
    return "\n".join(line for line in out if line.strip())


__all__ = [
    "browser_click_tool",
    "browser_download_tool",
    "browser_evaluate_tool",
    "browser_fill_tool",
    "browser_navigate_tool",
    "browser_screenshot_tool",
    "browser_snapshot_tool",
    "default_browser_tools",
]
