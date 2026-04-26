"""JSON-RPC methods for the dashboard-embedded canvas.

Wire surface (dashboard ↔ gateway):

- `canvas.present`     {agent_id, html, title?}     → ack + version
- `canvas.navigate`    {agent_id, url}              → only `data:` and
                                                       `about:blank` accepted
                                                       (dashboard-only output policy)
- `canvas.hide`        {agent_id}                   → ack
- `canvas.eval`        {agent_id, expression, timeout_seconds=5} → result
- `canvas.get_state`   {agent_id}                   → CanvasState dict
- `canvas.subscribe`   (server-pushed CanvasEvent stream — see GatewayServer)
- `canvas.eval_result` {request_id, ok, value?, error?} ← dashboard-side reply

The dashboard uses `canvas.subscribe` to learn about new state and runs
`canvas.eval_result` to feed back JS execution results.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from sampyclaw.canvas.errors import CanvasEvalError
from sampyclaw.canvas.events import CanvasEvent, CanvasEventBus
from sampyclaw.canvas.store import ABSOLUTE_MAX_HTML_BYTES, CanvasStore
from sampyclaw.gateway.router import Router
from sampyclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("gateway.canvas")


# Only navigation targets that cannot be remote sources are allowed,
# so the canvas can NEVER be a vehicle to point the dashboard at an
# external URL the model picked.
_ALLOWED_NAVIGATE_SCHEMES = ("data",)
_ALLOWED_NAVIGATE_LITERALS = ("about:blank",)


class _PresentParams(BaseModel):
    model_config = {"extra": "forbid"}
    agent_id: str = Field(..., min_length=1)
    html: str = Field(..., min_length=1)
    title: str = Field(default="")


class _NavigateParams(BaseModel):
    model_config = {"extra": "forbid"}
    agent_id: str = Field(..., min_length=1)
    url: str = Field(..., min_length=1)


class _HideParams(BaseModel):
    model_config = {"extra": "forbid"}
    agent_id: str = Field(..., min_length=1)


class _EvalParams(BaseModel):
    model_config = {"extra": "forbid"}
    agent_id: str = Field(..., min_length=1)
    expression: str = Field(..., min_length=1, max_length=8192)
    timeout_seconds: float = Field(default=5.0, gt=0, le=30.0)


class _EvalResultParams(BaseModel):
    model_config = {"extra": "forbid"}
    request_id: str = Field(..., min_length=1)
    ok: bool
    value: Any = None
    error: str | None = None


class _GetStateParams(BaseModel):
    model_config = {"extra": "forbid"}
    agent_id: str = Field(..., min_length=1)


def _is_dashboard_only_url(url: str) -> bool:
    if url in _ALLOWED_NAVIGATE_LITERALS:
        return True
    parsed = urlparse(url)
    return parsed.scheme in _ALLOWED_NAVIGATE_SCHEMES


def register_canvas_methods(
    router: Router,
    *,
    store: CanvasStore,
    bus: CanvasEventBus,
) -> None:
    @router.method("canvas.present", _PresentParams)
    async def _present(p: _PresentParams) -> dict[str, Any]:
        if len(p.html.encode("utf-8")) > ABSOLUTE_MAX_HTML_BYTES:
            return {
                "ok": False,
                "error": f"html exceeds canvas cap of {ABSOLUTE_MAX_HTML_BYTES} bytes",
            }
        state = store.present(p.agent_id, html=p.html, title=p.title)
        bus.publish(CanvasEvent(
            kind="present",
            agent_id=p.agent_id,
            payload={"html": p.html, "title": p.title, "version": state.version},
        ))
        return {"ok": True, "version": state.version, "subscribers": bus.subscriber_count}

    @router.method("canvas.navigate", _NavigateParams)
    async def _navigate(p: _NavigateParams) -> dict[str, Any]:
        if not _is_dashboard_only_url(p.url):
            return {
                "ok": False,
                "error": (
                    "canvas.navigate refuses non-dashboard URLs; "
                    "only data:... and about:blank are allowed."
                ),
            }
        bus.publish(CanvasEvent(kind="navigate", agent_id=p.agent_id, payload={"url": p.url}))
        return {"ok": True}

    @router.method("canvas.hide", _HideParams)
    async def _hide(p: _HideParams) -> dict[str, Any]:
        state = store.hide(p.agent_id)
        bus.publish(CanvasEvent(kind="hide", agent_id=p.agent_id, payload={}))
        return {"ok": True, "had_state": state is not None}

    @router.method("canvas.eval", _EvalParams)
    async def _eval(p: _EvalParams) -> dict[str, Any]:
        state = store.get(p.agent_id)
        if state is None or state.hidden:
            return {
                "ok": False,
                "error": (
                    f"no visible canvas for agent {p.agent_id!r}; "
                    f"call canvas.present first"
                ),
            }
        request_id = bus.new_eval_request_id()
        fut = bus.register_eval_waiter(request_id)
        bus.publish(CanvasEvent(
            kind="eval",
            agent_id=p.agent_id,
            request_id=request_id,
            payload={"expression": p.expression},
        ))
        try:
            value = await asyncio.wait_for(fut, timeout=p.timeout_seconds)
        except TimeoutError:
            bus.reject_eval(request_id, TimeoutError())
            return {
                "ok": False,
                "error": (
                    f"canvas.eval timed out after {p.timeout_seconds}s "
                    f"(no dashboard responded)"
                ),
            }
        except CanvasEvalError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "value": value}

    @router.method("canvas.eval_result", _EvalResultParams)
    async def _eval_result(p: _EvalResultParams) -> dict[str, Any]:
        if p.ok:
            delivered = bus.resolve_eval(p.request_id, p.value)
        else:
            delivered = bus.reject_eval(
                p.request_id, CanvasEvalError(p.error or "dashboard reported failure")
            )
        return {"delivered": delivered}

    @router.method("canvas.get_state", _GetStateParams)
    async def _get_state(p: _GetStateParams) -> dict[str, Any]:
        state = store.get(p.agent_id)
        if state is None:
            return {"ok": True, "state": None}
        return {"ok": True, "state": state.to_dict()}


__all__ = ["register_canvas_methods"]
