"""Shared fixtures for headless dashboard E2E tests.

The whole module is skipped (with a clear `reason`) when either:

1. `playwright` Python package is not installed — `pip install
   oxenclaw[dev,dashboard-tests]` or `pip install playwright pytest-asyncio`.
2. The bundled Chromium browser is missing — run `playwright install chromium`.
3. System libraries the bundled Chromium needs are missing — run
   `sudo playwright install-deps chromium` once on a Linux host (or
   `sudo apt-get install libnss3 libnspr4 libasound2t64` directly on
   Ubuntu 24.04). The first try will tell you which library is missing.

Each test gets:

- `gateway`: an in-process `GatewayServer` on a unique port with a
  unique auth token, returning the dashboard URL with `?token=...`.
- `page`: a Playwright Page bound to that URL, with `pageerror` /
  console-error listeners attached so JS errors fail the test.
"""

from __future__ import annotations

import asyncio
import secrets
import socket
import subprocess
import time
from collections.abc import AsyncIterator
from contextlib import closing
from pathlib import Path

import pytest

# ─── module-level skip gates ──────────────────────────────────────────


def _has_playwright() -> tuple[bool, str]:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        return False, "playwright not installed (`pip install playwright`)"
    return True, ""


def _chromium_runnable() -> tuple[bool, str]:
    """Quickly probe whether the bundled chromium can launch on this host.

    Spawns the binary with `--version`; OSError about missing shared
    libs is the failure mode we want to skip on.
    """
    home = Path.home() / ".cache" / "ms-playwright"
    if not home.exists():
        return False, "no playwright browsers installed (`playwright install chromium`)"
    candidates = list(home.glob("chromium-*/chrome-linux*/chrome"))
    candidates += list(
        home.glob("chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell")
    )
    if not candidates:
        return False, "chromium binary not found (`playwright install chromium`)"
    try:
        out = subprocess.run(
            [str(candidates[0]), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"chromium binary not runnable: {exc}"
    if out.returncode != 0:
        # Common case on a stripped Linux: missing libnspr4/libnss3/libasound2.
        msg = (out.stderr or out.stdout).strip().split("\n")[0]
        return False, (
            f"chromium can't launch ({msg}). Install system deps once: "
            f"`sudo playwright install-deps chromium`"
        )
    return True, ""


_PW_OK, _PW_REASON = _has_playwright()
_CR_OK, _CR_REASON = (False, "(playwright missing)") if not _PW_OK else _chromium_runnable()
SKIP_REASON = _PW_REASON or _CR_REASON


def pytest_collection_modifyitems(config, items):  # type: ignore[no-untyped-def]
    """Skip every test in this directory when browser deps are missing.

    `pytestmark` in conftest doesn't propagate to test modules, so the
    auto-skip lives here instead. The reason string tells the user
    exactly which `apt`/`playwright` command to run.
    """
    if _PW_OK and _CR_OK:
        return
    skip = pytest.mark.skip(reason=f"dashboard E2E unavailable: {SKIP_REASON}")
    for item in items:
        # Only mark items collected from this directory.
        if "tests/dashboard" in str(item.fspath).replace("\\", "/"):
            item.add_marker(skip)


# ─── gateway boot ─────────────────────────────────────────────────────


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def gateway(tmp_path) -> AsyncIterator[dict]:  # type: ignore[no-untyped-def]
    """Start an in-process gateway with a unique port + token.

    Yields a dict with `url`, `token`, `home`, `port`. Tears the gateway
    down on exit by triggering its graceful-shutdown event.
    """
    from oxenclaw.agents import (
        AgentRegistry,
        Dispatcher,
        EchoAgent,
    )
    from oxenclaw.approvals import ApprovalManager
    from oxenclaw.channels import ChannelRouter
    from oxenclaw.cli.gateway_cmd import _build_router
    from oxenclaw.config.paths import OxenclawPaths
    from oxenclaw.cron import CronJobStore, CronScheduler
    from oxenclaw.gateway import GatewayServer
    from oxenclaw.plugin_sdk.config_schema import RootConfig

    home = tmp_path / "home"
    home.mkdir()
    paths = OxenclawPaths(home=home)
    paths.ensure_home()

    agents = AgentRegistry()
    agents.register(EchoAgent())
    cr = ChannelRouter()
    config = RootConfig()
    dispatcher = Dispatcher(agents=agents, config=config, send=cr.send)
    cron = CronScheduler(
        store=CronJobStore(path=tmp_path / "cron.json"),
        dispatcher=dispatcher,
    )
    approvals = ApprovalManager()
    router = _build_router(
        agents=agents,
        dispatcher=dispatcher,
        channel_router=cr,
        cron_scheduler=cron,
        approvals=approvals,
        paths_home=paths,
    )

    token = secrets.token_hex(16)
    port = _free_port()
    server = GatewayServer(router=router, auth_token=token)

    serve_task = asyncio.create_task(server.serve(host="127.0.0.1", port=port))
    # Wait for the WS server to actually bind.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with closing(socket.create_connection(("127.0.0.1", port), timeout=0.2)):
                break
        except OSError:
            await asyncio.sleep(0.05)
    else:
        serve_task.cancel()
        raise RuntimeError(f"gateway did not bind to {port}")

    try:
        yield {
            "url": f"http://127.0.0.1:{port}",
            "url_with_token": f"http://127.0.0.1:{port}/?token={token}",
            "token": token,
            "home": home,
            "port": port,
        }
    finally:
        server.request_shutdown()
        try:
            await asyncio.wait_for(serve_task, timeout=3)
        except (TimeoutError, asyncio.CancelledError):
            serve_task.cancel()
        cron.stop()


# ─── browser fixtures ─────────────────────────────────────────────────


@pytest.fixture(scope="session")
async def _pw():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        yield pw


@pytest.fixture(scope="session")
async def _browser(_pw):  # type: ignore[no-untyped-def]
    browser = await _pw.chromium.launch()
    try:
        yield browser
    finally:
        await browser.close()


@pytest.fixture
async def page(_browser, gateway):  # type: ignore[no-untyped-def]
    """Open a fresh tab on the dashboard, fail the test on any JS error."""
    ctx = await _browser.new_context(viewport={"width": 1280, "height": 800})
    page = await ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on(
        "console",
        lambda msg: errors.append(f"console.error: {msg.text}") if msg.type == "error" else None,
    )
    page.goto_url = gateway["url_with_token"]  # type: ignore[attr-defined]
    page._js_errors = errors  # type: ignore[attr-defined]
    await page.goto(gateway["url_with_token"], wait_until="networkidle", timeout=15_000)
    yield page
    await ctx.close()
    if errors:
        pytest.fail("JS errors during test:\n  " + "\n  ".join(errors))
