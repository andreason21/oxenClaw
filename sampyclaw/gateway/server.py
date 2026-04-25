"""WebSocket JSON-RPC server with optional HTTP static serving on the same port.

Accepts JSON-encoded RpcRequest frames over WS, dispatches through `Router`,
and pushes server-initiated `EventFrame` messages out of a per-connection
queue. Plain-HTTP requests (no `Upgrade: websocket` header) are routed to a
small set of static handlers so the bundled dashboard can be served from
the same port the WS gateway listens on — handy for `http://localhost:7331/`.

Port of openclaw `src/gateway/server.ts`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
from collections.abc import Callable
from http import HTTPStatus
from typing import Any

from websockets import ConnectionClosed
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

from sampyclaw.gateway.protocol import EventFrame
from sampyclaw.gateway.router import Router
from sampyclaw.plugin_sdk.runtime_env import get_logger
from sampyclaw.static import app_css, app_html, app_js

logger = get_logger("gateway.server")

# 1 MiB inbound frame cap. Matches what a JSON-RPC payload should ever be;
# anything bigger is a misuse or an attack.
DEFAULT_MAX_MESSAGE_SIZE = 1 << 20
# Outbound event queue cap per connection. A slow client cannot make us
# accumulate unbounded memory.
DEFAULT_OUTBOUND_QUEUE_SIZE = 256
# Max concurrent in-flight RPCs on a single connection.
DEFAULT_PER_CONN_CONCURRENCY = 16


from collections.abc import Awaitable

StaticHandler = Callable[
    [ServerConnection, Request], "Response | Awaitable[Response]"
]


_CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
}


def _is_websocket_upgrade(request: Request) -> bool:
    upgrade = request.headers.get("Upgrade") or request.headers.get("upgrade") or ""
    return "websocket" in upgrade.lower()


def _typed_response(
    connection: ServerConnection, body: str, content_type: str
) -> Response:
    response = connection.respond(HTTPStatus.OK, body)
    # `Headers.__setitem__` appends, so we delete the default text/plain
    # entry first to avoid duplicate Content-Type lines on the wire.
    del response.headers["Content-Type"]
    response.headers["Content-Type"] = content_type
    response.headers["Cache-Control"] = "no-store"
    return response


def _content_type_for(path: str) -> str:
    for ext, ctype in _CONTENT_TYPES.items():
        if path.endswith(ext):
            return ctype
    return "application/octet-stream"


def default_static_routes(
    readiness: "ReadinessChecker | None" = None,
) -> dict[str, StaticHandler]:
    """Path → handler mapping served when a non-WS HTTP request arrives.

    Override or extend by passing `static_routes=` to `GatewayServer`.

    Operational endpoints:
      - `/healthz` — liveness; always 200 if the process responds.
      - `/readyz`  — readiness; 200 when all critical probes pass, 503
        otherwise. Body is JSON with the per-probe breakdown.
      - `/metrics` — Prometheus-format metrics.

    `/health` (legacy alias) maps to `/healthz` for back-compat.
    """
    from sampyclaw.observability import (
        ReadinessChecker,
        ReadinessStatus,
        render_prometheus,
    )

    def serve_app_html(connection: ServerConnection, _: Request) -> Response:
        return _typed_response(connection, app_html(), _content_type_for(".html"))

    def serve_app_css(connection: ServerConnection, _: Request) -> Response:
        return _typed_response(connection, app_css(), _content_type_for(".css"))

    def serve_app_js(connection: ServerConnection, _: Request) -> Response:
        return _typed_response(connection, app_js(), _content_type_for(".js"))

    def serve_healthz(connection: ServerConnection, _: Request) -> Response:
        return _typed_response(
            connection, "ok\n", _content_type_for(".txt")
        )

    async def serve_readyz(
        connection: ServerConnection, _: Request
    ) -> Response:
        if readiness is None:
            return _typed_response(
                connection,
                json.dumps({"status": "ok", "probes": []}) + "\n",
                _content_type_for(".json"),
            )
        report = await readiness.evaluate()
        body = json.dumps(report.to_dict()) + "\n"
        if report.is_ready():
            return _typed_response(
                connection, body, _content_type_for(".json")
            )
        response = connection.respond(
            HTTPStatus.SERVICE_UNAVAILABLE, body
        )
        del response.headers["Content-Type"]
        response.headers["Content-Type"] = _content_type_for(".json")
        response.headers["Cache-Control"] = "no-store"
        return response

    def serve_metrics(connection: ServerConnection, _: Request) -> Response:
        return _typed_response(
            connection,
            render_prometheus(),
            "text/plain; version=0.0.4; charset=utf-8",
        )

    return {
        "/": serve_app_html,
        "/dashboard": serve_app_html,
        "/dashboard.html": serve_app_html,
        "/app.html": serve_app_html,
        "/static/app.css": serve_app_css,
        "/static/app.js": serve_app_js,
        "/health": serve_healthz,  # legacy alias
        "/healthz": serve_healthz,
        "/readyz": serve_readyz,
        "/metrics": serve_metrics,
    }


def _resolve_auth_token(explicit: str | None) -> str | None:
    """Resolve the gateway bearer token.

    Precedence: explicit constructor argument → `SAMPYCLAW_GATEWAY_TOKEN`
    environment variable → None (auth disabled, with a loud warning).
    """
    if explicit:
        return explicit
    env = os.environ.get("SAMPYCLAW_GATEWAY_TOKEN")
    if env:
        return env
    return None


# HTTP routes that are deliberately unauthenticated:
#  - /healthz, /readyz, /metrics, /health (operational probes hit by
#    orchestrators that don't carry the gateway token).
# Everything else under static_routes (the dashboard + its CSS/JS bundle
# + any user-supplied custom route) requires the same token the WS
# upgrade does.
PUBLIC_HTTP_PATHS: frozenset[str] = frozenset(
    {"/healthz", "/readyz", "/metrics", "/health"}
)


def _query_token(request: Request) -> str | None:
    """Extract the `?token=...` query value (if any), no header fallback."""
    if "?" not in request.path:
        return None
    from urllib.parse import parse_qs

    qs = parse_qs(request.path.split("?", 1)[1])
    return qs.get("token", [None])[0]


def _query_token_present(request: Request) -> bool:
    return _query_token(request) is not None


def _set_token_cookie(response: Response, token: str | None) -> None:
    """Set the `sampyclaw_token` cookie on `response`. No-op if `token` is
    None or empty. The cookie is `HttpOnly=false` because the dashboard
    JS reads it back to assemble the WS URL."""
    if not token:
        return
    cookie = (
        f"{TOKEN_COOKIE_NAME}={token}"
        f"; Max-Age={TOKEN_COOKIE_MAX_AGE_SECONDS}"
        "; Path=/"
        "; SameSite=Strict"
    )
    response.headers["Set-Cookie"] = cookie



# Cookie used by the dashboard to remember a token resolved from the
# initial `?token=...` URL so reloads / bookmarks Just Work without
# leaving the secret in the address bar.
TOKEN_COOKIE_NAME = "sampyclaw_token"
TOKEN_COOKIE_MAX_AGE_SECONDS = 12 * 3600  # 12h — short enough to limit
# stolen-cookie blast radius without forcing a re-login every refresh.


def _bearer_from_request(request: Request) -> str | None:
    """Extract a bearer token from `Authorization`, `?token=`, or the
    `sampyclaw_token` cookie."""
    auth = (
        request.headers.get("Authorization")
        or request.headers.get("authorization")
        or ""
    )
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    # Browsers can't set Authorization on a WS upgrade or a top-level
    # navigation — fall back to query string.
    path = request.path
    if "?" in path:
        from urllib.parse import parse_qs

        qs = parse_qs(path.split("?", 1)[1])
        token = qs.get("token", [None])[0]
        if token:
            return token
    # And finally a cookie set by the dashboard after the first
    # `?token=` load — keeps reloads from re-prompting.
    cookie_header = (
        request.headers.get("Cookie") or request.headers.get("cookie") or ""
    )
    if cookie_header:
        for pair in cookie_header.split(";"):
            name, _, value = pair.strip().partition("=")
            if name == TOKEN_COOKIE_NAME and value:
                return value
    return None


class ConnectionContext:
    """Per-connection state: outbound event queue + concurrency cap."""

    def __init__(
        self,
        *,
        outbound_queue_size: int = DEFAULT_OUTBOUND_QUEUE_SIZE,
        max_concurrency: int = DEFAULT_PER_CONN_CONCURRENCY,
    ) -> None:
        self.events: asyncio.Queue[EventFrame] = asyncio.Queue(
            maxsize=outbound_queue_size
        )
        self.in_flight: asyncio.Semaphore = asyncio.Semaphore(max_concurrency)

    async def push_event(self, event: EventFrame) -> bool:
        """Try to enqueue. Returns False (and drops) when full — caller logs."""
        try:
            self.events.put_nowait(event)
            return True
        except asyncio.QueueFull:
            return False


class GatewayServer:
    def __init__(
        self,
        router: Router,
        *,
        static_routes: dict[str, StaticHandler] | None = None,
        auth_token: str | None = None,
        max_message_size: int = DEFAULT_MAX_MESSAGE_SIZE,
        outbound_queue_size: int = DEFAULT_OUTBOUND_QUEUE_SIZE,
        per_connection_concurrency: int = DEFAULT_PER_CONN_CONCURRENCY,
        shutdown_drain_seconds: float = 10.0,
        readiness=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self._router = router
        self._connections: set[ConnectionContext] = set()
        self._readiness = readiness
        self._static_routes = (
            default_static_routes(readiness=readiness)
            if static_routes is None
            else dict(static_routes)
        )
        self._auth_token = _resolve_auth_token(auth_token)
        self._max_message_size = max_message_size
        self._outbound_queue_size = outbound_queue_size
        self._per_conn_concurrency = per_connection_concurrency
        self._shutdown_drain_seconds = shutdown_drain_seconds
        self._shutdown_event: asyncio.Event | None = None
        self._shutting_down = False
        self._in_flight_tasks: set[asyncio.Task[None]] = set()
        if self._auth_token is None:
            logger.warning(
                "gateway started WITHOUT auth — set SAMPYCLAW_GATEWAY_TOKEN "
                "or pass auth_token= to require Authorization: Bearer on connect"
            )

    @property
    def connections(self) -> set[ConnectionContext]:
        return self._connections

    @property
    def static_routes(self) -> dict[str, StaticHandler]:
        return self._static_routes

    async def broadcast(self, event: EventFrame) -> None:
        for ctx in list(self._connections):
            if not await ctx.push_event(event):
                logger.warning("dropped event — outbound queue full for one client")

    async def serve(self, host: str = "127.0.0.1", port: int = 7331) -> None:
        self._shutdown_event = asyncio.Event()
        async with serve(
            self._on_connect,
            host,
            port,
            process_request=self._process_request,
            max_size=self._max_message_size,
        ) as ws_server:
            logger.info("gateway listening on http://%s:%d", host, port)
            await self._shutdown_event.wait()
            logger.info(
                "gateway shutdown signaled — draining %d in-flight RPC(s) "
                "across %d connection(s) (timeout=%.1fs)",
                len(self._in_flight_tasks),
                len(self._connections),
                self._shutdown_drain_seconds,
            )
            # 1) Wait for in-flight RPC handlers to finish (or timeout). New
            #    RPCs are already refused — `_on_connect` checks
            #    `_shutting_down` before scheduling further work.
            await self._drain_in_flight()
            # 2) Close the WS server: this terminates the `async for raw in
            #    ws` loops in every connection handler, so `_on_connect`
            #    can return and the server exits the context cleanly.
            ws_server.close()
            with contextlib.suppress(Exception):
                await ws_server.wait_closed()
        logger.info("gateway listener closed")

    def request_shutdown(self) -> None:
        """Idempotent: ask `serve()` to return cleanly.

        Safe to call from a signal handler — only sets an asyncio.Event that
        the server loop awaits.
        """
        self._shutting_down = True
        if self._shutdown_event is not None and not self._shutdown_event.is_set():
            self._shutdown_event.set()

    async def _drain_in_flight(self) -> None:
        if not self._in_flight_tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *self._in_flight_tasks, return_exceptions=True
                ),
                timeout=self._shutdown_drain_seconds,
            )
        except asyncio.TimeoutError:
            still_running = sum(
                1 for t in self._in_flight_tasks if not t.done()
            )
            logger.warning(
                "drain timeout: cancelling %d in-flight RPC task(s)",
                still_running,
            )
            for t in self._in_flight_tasks:
                if not t.done():
                    t.cancel()

    async def _process_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        # WebSocket upgrade requests: enforce auth here so we reject before
        # the handshake completes.
        if _is_websocket_upgrade(request):
            if not self._auth_ok(request):
                logger.warning("rejecting WS upgrade: missing/invalid bearer token")
                return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
            return None
        path = request.path.split("?", 1)[0]
        # Static routes (dashboard HTML / CSS / JS / health probes) all
        # load unauthenticated. The dashboard SPA itself renders a login
        # gate when no token is detected and uses it to authenticate the
        # WS upgrade — matches openclaw's `control-ui` UX.
        # We *do* still validate `?token=` if present, so that pasting
        # `http://host/?token=...` continues to work as a one-shot login
        # (the gateway sets a cookie so the SPA picks it up on reload).
        handler = self._static_routes.get(path)
        if handler is not None:
            try:
                result = handler(connection, request)
                if asyncio.iscoroutine(result):
                    result = await result
                if (
                    self._auth_token is not None
                    and _query_token_present(request)
                    and self._auth_ok(request)
                    and result is not None
                ):
                    _set_token_cookie(result, _query_token(request))
                return result
            except Exception:
                logger.exception("static handler %s raised", path)
                return connection.respond(
                    HTTPStatus.INTERNAL_SERVER_ERROR, "static handler error\n"
                )
        return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")

    def _auth_ok(self, request: Request) -> bool:
        if self._auth_token is None:
            return True
        offered = _bearer_from_request(request)
        if offered is None:
            return False
        return hmac.compare_digest(offered, self._auth_token)

    async def _on_connect(self, ws: ServerConnection) -> None:
        from sampyclaw.observability import METRICS

        ctx = ConnectionContext(
            outbound_queue_size=self._outbound_queue_size,
            max_concurrency=self._per_conn_concurrency,
        )
        self._connections.add(ctx)
        METRICS.ws_connections_active.set(len(self._connections))
        sender = asyncio.create_task(self._pump_events(ws, ctx))
        in_flight: set[asyncio.Task[None]] = set()
        try:
            async for raw in ws:
                if self._shutting_down:
                    # Stop accepting new RPCs once shutdown has begun.
                    break
                task = asyncio.create_task(self._handle_message(ws, ctx, raw))
                in_flight.add(task)
                self._in_flight_tasks.add(task)

                def _on_done(t: asyncio.Task[None]) -> None:
                    in_flight.discard(t)
                    self._in_flight_tasks.discard(t)

                task.add_done_callback(_on_done)
        except ConnectionClosed:
            pass
        finally:
            # During graceful shutdown we let in-flight tasks finish naturally
            # (they're tracked at server level and drained by `serve`). On a
            # forced disconnect we cancel them — work that the client will no
            # longer see is wasted.
            if not self._shutting_down:
                for t in in_flight:
                    t.cancel()
            sender.cancel()
            self._connections.discard(ctx)
            METRICS.ws_connections_active.set(len(self._connections))

    async def _handle_message(
        self, ws: ServerConnection, ctx: ConnectionContext, raw: str | bytes
    ) -> None:
        from sampyclaw.observability import (
            METRICS,
            correlation_scope,
            new_correlation_id,
        )

        async with ctx.in_flight:
            try:
                payload: Any = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("ignoring non-JSON frame")
                return
            if not isinstance(payload, dict):
                logger.warning("ignoring non-object frame")
                return
            method = (
                payload.get("method")
                if isinstance(payload.get("method"), str)
                else "unknown"
            )
            label = {"method": method}
            trace_id = new_correlation_id()
            with correlation_scope(trace_id=trace_id, rpc=method):
                METRICS.ws_rpc_total.inc(labels=label)
                start = asyncio.get_event_loop().time()
                response = await self._router.dispatch(payload)
                METRICS.ws_rpc_duration_seconds.observe(
                    asyncio.get_event_loop().time() - start, labels=label
                )
                payload_dict = response.model_dump(exclude_none=True)
                if "error" in payload_dict:
                    METRICS.ws_rpc_errors_total.inc(labels=label)
                try:
                    await ws.send(response.model_dump_json(exclude_none=True))
                except ConnectionClosed:
                    pass

    async def _pump_events(self, ws: ServerConnection, ctx: ConnectionContext) -> None:
        try:
            while True:
                event = await ctx.events.get()
                await ws.send(event.model_dump_json())
        except (asyncio.CancelledError, ConnectionClosed):
            pass
