"""HTTP static serving on the gateway port — dashboard at /, health at /health,
plus a regression check that WS upgrade still works on the same port."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from urllib.request import Request, urlopen

import pytest
from websockets.asyncio.client import connect

from oxenclaw.gateway import (
    ChatSendParams,
    ChatSendResult,
    GatewayServer,
    Router,
)


def _pick_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def router() -> Router:
    r = Router()

    @r.method("chat.send", ChatSendParams)
    async def _send(p: ChatSendParams) -> ChatSendResult:
        return ChatSendResult(message_id=f"{p.chat_id}:sent", timestamp=1.0)

    return r


def _http_get(url: str) -> tuple[int, dict, bytes]:
    req = Request(url, method="GET")
    try:
        with urlopen(req, timeout=2) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except Exception as exc:  # urllib raises HTTPError on non-2xx
        if hasattr(exc, "code"):
            return (
                exc.code,
                dict(getattr(exc, "headers", {}) or {}),
                getattr(  # type: ignore[union-attr]
                    exc, "read", lambda: b""
                )(),
            )
        raise


async def _run_with_server(router: Router, fn) -> None:  # type: ignore[no-untyped-def]
    server = GatewayServer(router)
    port = _pick_port()
    task = asyncio.create_task(server.serve(host="127.0.0.1", port=port))
    try:
        await asyncio.sleep(0.1)
        await fn(port, server)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_dashboard_root_serves_html(router: Router) -> None:
    async def _check(port: int, _server: GatewayServer) -> None:
        status, headers, body = await asyncio.to_thread(_http_get, f"http://127.0.0.1:{port}/")
        assert status == 200
        assert "text/html" in headers.get("Content-Type", "")
        assert b"<!DOCTYPE html>" in body[:64]
        assert b"app.js" in body  # SPA shell loads the JS bundle

    await _run_with_server(router, _check)


async def test_dashboard_alias_paths(router: Router) -> None:
    async def _check(port: int, _server: GatewayServer) -> None:
        for path in ("/dashboard", "/dashboard.html", "/app.html"):
            status, _, body = await asyncio.to_thread(_http_get, f"http://127.0.0.1:{port}{path}")
            assert status == 200, path
            assert b"<!DOCTYPE html>" in body[:64], path

    await _run_with_server(router, _check)


async def test_static_css_and_js(router: Router) -> None:
    async def _check(port: int, _server: GatewayServer) -> None:
        status, headers, body = await asyncio.to_thread(
            _http_get, f"http://127.0.0.1:{port}/static/app.css"
        )
        assert status == 200
        assert "text/css" in headers.get("Content-Type", "")
        assert len(body) > 0

        status_js, headers_js, body_js = await asyncio.to_thread(
            _http_get, f"http://127.0.0.1:{port}/static/app.js"
        )
        assert status_js == 200
        assert "javascript" in headers_js.get("Content-Type", "")
        assert len(body_js) > 0

    await _run_with_server(router, _check)


async def test_health_endpoint(router: Router) -> None:
    async def _check(port: int, _server: GatewayServer) -> None:
        # `/health` is a legacy alias.
        status, _, body = await asyncio.to_thread(_http_get, f"http://127.0.0.1:{port}/health")
        assert status == 200
        assert b"ok" in body

        # `/healthz` is the canonical liveness endpoint.
        status, _, body = await asyncio.to_thread(_http_get, f"http://127.0.0.1:{port}/healthz")
        assert status == 200
        assert b"ok" in body

    await _run_with_server(router, _check)


async def test_readyz_without_checker_returns_ok(router: Router) -> None:
    async def _check(port: int, _server: GatewayServer) -> None:
        status, headers, body = await asyncio.to_thread(
            _http_get, f"http://127.0.0.1:{port}/readyz"
        )
        assert status == 200
        assert "json" in headers.get("Content-Type", "")
        decoded = json.loads(body)
        assert decoded["status"] == "ok"
        assert decoded["probes"] == []

    await _run_with_server(router, _check)


async def test_readyz_returns_503_when_critical_probe_fails(
    router: Router,
) -> None:
    from oxenclaw.observability import ReadinessChecker, ReadinessStatus

    checker = ReadinessChecker()

    async def down():
        return ReadinessStatus.DOWN, "broken"

    checker.register_check("db", down, critical=True)

    server = GatewayServer(router, readiness=checker)
    port = _pick_port()
    task = asyncio.create_task(server.serve(host="127.0.0.1", port=port))
    try:
        await asyncio.sleep(0.1)
        status, _headers, body = await asyncio.to_thread(
            _http_get, f"http://127.0.0.1:{port}/readyz"
        )
        assert status == 503
        decoded = json.loads(body)
        assert decoded["status"] == "down"
        assert decoded["probes"][0]["name"] == "db"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_metrics_endpoint_returns_prometheus_text(router: Router) -> None:
    async def _check(port: int, _server: GatewayServer) -> None:
        status, headers, body = await asyncio.to_thread(
            _http_get, f"http://127.0.0.1:{port}/metrics"
        )
        assert status == 200
        ctype = headers.get("Content-Type", "")
        assert "text/plain" in ctype
        text = body.decode("utf-8")
        # The registry exposes our standard counters even at zero.
        assert "TYPE oxenclaw_ws_rpc_total counter" in text
        assert "TYPE oxenclaw_ws_connections_active gauge" in text

    await _run_with_server(router, _check)


async def test_metrics_increments_after_ws_rpc(router: Router) -> None:
    """A real WS RPC should bump `ws_rpc_total{method="chat.send"}`."""
    from oxenclaw.observability import METRICS

    METRICS.ws_rpc_total._values.clear()  # reset counter for this test

    async def _check(port: int, _server: GatewayServer) -> None:
        async with connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "chat.send",
                        "params": {
                            "channel": "telegram",
                            "account_id": "main",
                            "chat_id": "1",
                            "text": "hi",
                        },
                    }
                )
            )
            await asyncio.wait_for(ws.recv(), timeout=2.0)

        await asyncio.sleep(0.05)
        status, _, body = await asyncio.to_thread(_http_get, f"http://127.0.0.1:{port}/metrics")
        assert status == 200
        text = body.decode("utf-8")
        assert 'oxenclaw_ws_rpc_total{method="chat.send"} 1.0' in text

    await _run_with_server(router, _check)


async def test_unknown_path_404(router: Router) -> None:
    async def _check(port: int, _server: GatewayServer) -> None:
        status, _, _ = await asyncio.to_thread(_http_get, f"http://127.0.0.1:{port}/no-such-thing")
        assert status == 404

    await _run_with_server(router, _check)


async def test_websocket_upgrade_still_works(router: Router) -> None:
    """Adding HTTP static handling must not break the WS handshake."""

    async def _check(port: int, _server: GatewayServer) -> None:
        async with connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "chat.send",
                        "params": {
                            "channel": "telegram",
                            "account_id": "main",
                            "chat_id": "9",
                            "text": "hi",
                        },
                    }
                )
            )
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            assert json.loads(raw)["result"]["message_id"] == "9:sent"

    await _run_with_server(router, _check)


async def test_custom_static_routes(router: Router) -> None:
    from http import HTTPStatus

    def custom(connection, request):  # type: ignore[no-untyped-def]
        return connection.respond(HTTPStatus.OK, "custom!\n")

    server = GatewayServer(router, static_routes={"/custom": custom})
    assert server.static_routes == {"/custom": custom}
    port = _pick_port()
    task = asyncio.create_task(server.serve(host="127.0.0.1", port=port))
    try:
        await asyncio.sleep(0.1)
        status, _, body = await asyncio.to_thread(_http_get, f"http://127.0.0.1:{port}/custom")
        assert status == 200
        assert b"custom!" in body
        # Default routes are NOT inherited when caller supplies their own.
        status404, _, _ = await asyncio.to_thread(_http_get, f"http://127.0.0.1:{port}/")
        assert status404 == 404
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_static_assets_loadable_from_package() -> None:
    from oxenclaw.static import app_css, app_html, app_js, dashboard_html

    html = app_html()
    assert "<!DOCTYPE html>" in html[:64]
    assert "app.js" in html
    # Compatibility helper still resolves to the same shell.
    assert dashboard_html() == html
    assert len(app_css()) > 0
    assert len(app_js()) > 0


# ─── dashboard auth gate ─────────────────────────────────────────────


def _http_get_full(url: str, *, headers: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    """GET with optional headers; returns (status, headers, body) and never
    raises on non-2xx."""
    req = Request(url, method="GET", headers=headers or {})
    try:
        with urlopen(req, timeout=2) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except Exception as exc:
        if hasattr(exc, "code"):
            body = b""
            try:
                body = exc.read()  # type: ignore[union-attr]
            except Exception:
                pass
            return exc.code, dict(getattr(exc, "headers", {}) or {}), body  # type: ignore[union-attr]
        raise


async def test_dashboard_html_loads_unauthenticated(
    router: Router,
) -> None:
    """The SPA renders a login gate when no token is found, so the HTML
    itself must load anonymously (matches openclaw's control-ui UX)."""
    server = GatewayServer(router, auth_token="secret123")
    port = _pick_port()
    task = asyncio.create_task(server.serve(host="127.0.0.1", port=port))
    try:
        await asyncio.sleep(0.1)
        status, _, body = await asyncio.to_thread(_http_get, f"http://127.0.0.1:{port}/")
        assert status == 200
        assert b"<!DOCTYPE html>" in body[:64]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_dashboard_accepts_query_token_and_sets_cookie(
    router: Router,
) -> None:
    server = GatewayServer(router, auth_token="secret123")
    port = _pick_port()
    task = asyncio.create_task(server.serve(host="127.0.0.1", port=port))
    try:
        await asyncio.sleep(0.1)
        status, headers, body = await asyncio.to_thread(
            _http_get_full,
            f"http://127.0.0.1:{port}/?token=secret123",
        )
        assert status == 200
        assert b"<!DOCTYPE html>" in body[:64]
        # Token gets persisted into a cookie.
        cookie = headers.get("Set-Cookie") or headers.get("set-cookie")
        assert cookie is not None
        assert "oxenclaw_token=secret123" in cookie
        assert "SameSite=Strict" in cookie
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_dashboard_query_with_wrong_token_does_not_set_cookie(
    router: Router,
) -> None:
    """Anonymous serves still happen, but the cookie is only set when the
    `?token=` matches."""
    server = GatewayServer(router, auth_token="secret123")
    port = _pick_port()
    task = asyncio.create_task(server.serve(host="127.0.0.1", port=port))
    try:
        await asyncio.sleep(0.1)
        status, headers, _ = await asyncio.to_thread(
            _http_get_full,
            f"http://127.0.0.1:{port}/?token=wrong",
        )
        assert status == 200  # SPA still loads — login form renders
        assert "Set-Cookie" not in headers and "set-cookie" not in headers
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_static_assets_load_anonymously(router: Router) -> None:
    """CSS/JS must always load — the SPA needs them to render the login
    form when there's no token."""
    server = GatewayServer(router, auth_token="secret123")
    port = _pick_port()
    task = asyncio.create_task(server.serve(host="127.0.0.1", port=port))
    try:
        await asyncio.sleep(0.1)
        for path in ("/static/app.css", "/static/app.js"):
            status, _, body = await asyncio.to_thread(
                _http_get_full, f"http://127.0.0.1:{port}{path}"
            )
            assert status == 200, f"{path} should load anonymously"
            assert len(body) > 0
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_health_metrics_remain_unauthenticated(router: Router) -> None:
    """Operational probes must NOT require a token even when auth is on
    — orchestrators (k8s, systemd) probe these without credentials."""
    server = GatewayServer(router, auth_token="secret123")
    port = _pick_port()
    task = asyncio.create_task(server.serve(host="127.0.0.1", port=port))
    try:
        await asyncio.sleep(0.1)
        for path in ("/healthz", "/readyz", "/metrics", "/health"):
            status, _, _ = await asyncio.to_thread(_http_get_full, f"http://127.0.0.1:{port}{path}")
            assert status == 200, f"{path} should not require token"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_ws_upgrade_still_requires_token_when_auth_configured(
    router: Router,
) -> None:
    """The HTML/JS are public, but the WS upgrade is the actual auth
    boundary. Without a valid token the upgrade is refused."""
    from websockets.asyncio.client import connect as ws_connect
    from websockets.exceptions import InvalidStatus

    server = GatewayServer(router, auth_token="secret123")
    port = _pick_port()
    task = asyncio.create_task(server.serve(host="127.0.0.1", port=port))
    try:
        await asyncio.sleep(0.1)
        # No token → the upgrade is rejected with 401.
        with pytest.raises((InvalidStatus, ConnectionError, OSError)):
            async with ws_connect(f"ws://127.0.0.1:{port}/"):
                pass
        # With the right token in the WS URL → upgrade succeeds.
        async with ws_connect(f"ws://127.0.0.1:{port}/?token=secret123") as ws:
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "chat.send",
                        "params": {
                            "channel": "telegram",
                            "account_id": "main",
                            "chat_id": "1",
                            "text": "hi",
                        },
                    }
                )
            )
            await asyncio.wait_for(ws.recv(), timeout=2.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ─── Origin allowlist (CSRF defence on WS upgrade) ────────────────────


async def test_ws_upgrade_rejected_for_unlisted_origin(router: Router) -> None:
    """When `allowed_origins` is set, a browser-style WS upgrade with a
    foreign Origin must be rejected with 403 — even if the bearer token
    is correct."""
    from websockets.asyncio.client import connect as ws_connect
    from websockets.exceptions import InvalidStatus

    server = GatewayServer(
        router,
        auth_token="secret123",
        allowed_origins=["tauri://localhost"],
    )
    port = _pick_port()
    task = asyncio.create_task(server.serve(host="127.0.0.1", port=port))
    try:
        await asyncio.sleep(0.1)
        # Foreign origin → 403 even with the right token.
        with pytest.raises((InvalidStatus, ConnectionError, OSError)):
            async with ws_connect(
                f"ws://127.0.0.1:{port}/?token=secret123",
                additional_headers=[("Origin", "https://evil.example")],
            ):
                pass
        # Allowed origin → upgrade succeeds.
        async with ws_connect(
            f"ws://127.0.0.1:{port}/?token=secret123",
            additional_headers=[("Origin", "tauri://localhost")],
        ) as ws:
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "chat.send",
                        "params": {
                            "channel": "telegram",
                            "account_id": "main",
                            "chat_id": "1",
                            "text": "hi",
                        },
                    }
                )
            )
            await asyncio.wait_for(ws.recv(), timeout=2.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_ws_upgrade_without_origin_passes_when_allowlist_set(router: Router) -> None:
    """Non-browser clients (no Origin header at all) must still pass the
    Origin check — Origin filtering is a CSRF defence, not a token
    substitute. Native apps and `curl` don't send Origin."""
    from websockets.asyncio.client import connect as ws_connect

    server = GatewayServer(
        router,
        auth_token="secret123",
        allowed_origins=["tauri://localhost"],
    )
    port = _pick_port()
    task = asyncio.create_task(server.serve(host="127.0.0.1", port=port))
    try:
        await asyncio.sleep(0.1)
        # No Origin header at all + valid token → upgrade succeeds.
        async with ws_connect(f"ws://127.0.0.1:{port}/?token=secret123") as ws:
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "chat.send",
                        "params": {"channel": "t", "account_id": "m", "chat_id": "1", "text": "x"},
                    }
                )
            )
            await asyncio.wait_for(ws.recv(), timeout=2.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_allowed_origins_normalise_trailing_slash(router: Router) -> None:
    """Allowlist entries `'http://localhost:7331/'` should match the browser-emitted
    `'http://localhost:7331'` — the resolver strips trailing slashes."""
    from websockets.asyncio.client import connect as ws_connect

    server = GatewayServer(
        router,
        auth_token="secret123",
        allowed_origins=["http://localhost:7331/  "],  # space + slash, both stripped
    )
    port = _pick_port()
    task = asyncio.create_task(server.serve(host="127.0.0.1", port=port))
    try:
        await asyncio.sleep(0.1)
        async with ws_connect(
            f"ws://127.0.0.1:{port}/?token=secret123",
            additional_headers=[("Origin", "http://localhost:7331")],
        ) as ws:
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "chat.send",
                        "params": {"channel": "t", "account_id": "m", "chat_id": "1", "text": "x"},
                    }
                )
            )
            await asyncio.wait_for(ws.recv(), timeout=2.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_allowed_origins_resolved_from_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`OXENCLAW_ALLOWED_ORIGINS` env feeds the allowlist when the kwarg
    isn't passed."""
    monkeypatch.setenv("OXENCLAW_ALLOWED_ORIGINS", "tauri://localhost, http://localhost:7331")
    from oxenclaw.gateway.server import _resolve_allowed_origins

    out = _resolve_allowed_origins(None)
    assert out == frozenset({"tauri://localhost", "http://localhost:7331"})


def test_allowed_origins_explicit_kwarg_overrides_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OXENCLAW_ALLOWED_ORIGINS", "ignored://from-env")
    from oxenclaw.gateway.server import _resolve_allowed_origins

    out = _resolve_allowed_origins(["only://this"])
    assert out == frozenset({"only://this"})


def test_allowed_origins_empty_means_no_check() -> None:
    from oxenclaw.gateway.server import _resolve_allowed_origins

    assert _resolve_allowed_origins(None) is None
    assert _resolve_allowed_origins([]) is None
    assert _resolve_allowed_origins(["", "   "]) is None
