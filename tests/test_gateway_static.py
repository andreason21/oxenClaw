"""HTTP static serving on the gateway port — dashboard at /, health at /health,
plus a regression check that WS upgrade still works on the same port."""

from __future__ import annotations

import asyncio
import json
import socket
from urllib.request import Request, urlopen

import pytest
from websockets.asyncio.client import connect

from sampyclaw.gateway import (
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
            return exc.code, dict(getattr(exc, "headers", {}) or {}), getattr(  # type: ignore[union-attr]
                exc, "read", lambda: b""
            )()
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
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_dashboard_root_serves_html(router: Router) -> None:
    async def _check(port: int, _server: GatewayServer) -> None:
        status, headers, body = await asyncio.to_thread(
            _http_get, f"http://127.0.0.1:{port}/"
        )
        assert status == 200
        assert "text/html" in headers.get("Content-Type", "")
        assert b"<!DOCTYPE html>" in body[:64]
        assert b"app.js" in body  # SPA shell loads the JS bundle

    await _run_with_server(router, _check)


async def test_dashboard_alias_paths(router: Router) -> None:
    async def _check(port: int, _server: GatewayServer) -> None:
        for path in ("/dashboard", "/dashboard.html", "/app.html"):
            status, _, body = await asyncio.to_thread(
                _http_get, f"http://127.0.0.1:{port}{path}"
            )
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
        status, _, body = await asyncio.to_thread(
            _http_get, f"http://127.0.0.1:{port}/health"
        )
        assert status == 200
        assert b"ok" in body

        # `/healthz` is the canonical liveness endpoint.
        status, _, body = await asyncio.to_thread(
            _http_get, f"http://127.0.0.1:{port}/healthz"
        )
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
    from sampyclaw.observability import ReadinessChecker, ReadinessStatus

    checker = ReadinessChecker()

    async def down():
        return ReadinessStatus.DOWN, "broken"

    checker.register_check("db", down, critical=True)

    server = GatewayServer(router, readiness=checker)
    port = _pick_port()
    task = asyncio.create_task(server.serve(host="127.0.0.1", port=port))
    try:
        await asyncio.sleep(0.1)
        status, headers, body = await asyncio.to_thread(
            _http_get, f"http://127.0.0.1:{port}/readyz"
        )
        assert status == 503
        decoded = json.loads(body)
        assert decoded["status"] == "down"
        assert decoded["probes"][0]["name"] == "db"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


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
        assert "TYPE sampyclaw_ws_rpc_total counter" in text
        assert "TYPE sampyclaw_ws_connections_active gauge" in text


    await _run_with_server(router, _check)


async def test_metrics_increments_after_ws_rpc(router: Router) -> None:
    """A real WS RPC should bump `ws_rpc_total{method="chat.send"}`."""
    from sampyclaw.observability import METRICS

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
        status, _, body = await asyncio.to_thread(
            _http_get, f"http://127.0.0.1:{port}/metrics"
        )
        assert status == 200
        text = body.decode("utf-8")
        assert 'sampyclaw_ws_rpc_total{method="chat.send"} 1.0' in text

    await _run_with_server(router, _check)


async def test_unknown_path_404(router: Router) -> None:
    async def _check(port: int, _server: GatewayServer) -> None:
        status, _, _ = await asyncio.to_thread(
            _http_get, f"http://127.0.0.1:{port}/no-such-thing"
        )
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
        status, _, body = await asyncio.to_thread(
            _http_get, f"http://127.0.0.1:{port}/custom"
        )
        assert status == 200
        assert b"custom!" in body
        # Default routes are NOT inherited when caller supplies their own.
        status404, _, _ = await asyncio.to_thread(
            _http_get, f"http://127.0.0.1:{port}/"
        )
        assert status404 == 404
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def test_static_assets_loadable_from_package() -> None:
    from sampyclaw.static import app_css, app_html, app_js, dashboard_html

    html = app_html()
    assert "<!DOCTYPE html>" in html[:64]
    assert "app.js" in html
    # Compatibility helper still resolves to the same shell.
    assert dashboard_html() == html
    assert len(app_css()) > 0
    assert len(app_js()) > 0
