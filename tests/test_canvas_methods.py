"""Tests for sampyclaw.gateway.canvas_methods."""

from __future__ import annotations

import asyncio

import pytest

from sampyclaw.canvas import CanvasEventBus, CanvasStore
from sampyclaw.gateway.canvas_methods import register_canvas_methods
from sampyclaw.gateway.router import Router


def _setup() -> tuple[Router, CanvasStore, CanvasEventBus]:
    router = Router()
    store = CanvasStore()
    bus = CanvasEventBus()
    register_canvas_methods(router, store=store, bus=bus)
    return router, store, bus


async def _call(router: Router, method: str, params: dict) -> dict:
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
    )
    return resp


@pytest.mark.asyncio
async def test_present_stores_and_fans_out() -> None:
    router, store, bus = _setup()
    q = await bus.subscribe()
    resp = await _call(
        router,
        "canvas.present",
        {
            "agent_id": "a",
            "html": "<!doctype html><p>x</p>",
            "title": "T",
        },
    )
    assert resp.error is None
    assert resp.result["ok"] is True
    assert store.get("a").html == "<!doctype html><p>x</p>"
    evt = await asyncio.wait_for(q.get(), timeout=1.0)
    assert evt.kind == "present"


@pytest.mark.asyncio
async def test_present_refuses_oversized_html() -> None:
    router, _, _ = _setup()
    big = "x" * (1_048_576 + 1)
    resp = await _call(router, "canvas.present", {"agent_id": "a", "html": big})
    assert resp.error is None
    assert resp.result["ok"] is False
    assert "exceeds" in resp.result["error"].lower()


@pytest.mark.asyncio
async def test_hide_acks() -> None:
    router, store, _ = _setup()
    await _call(router, "canvas.present", {"agent_id": "a", "html": "<p>x</p>"})
    resp = await _call(router, "canvas.hide", {"agent_id": "a"})
    assert resp.result == {"ok": True, "had_state": True}
    assert store.get("a").hidden is True


@pytest.mark.asyncio
async def test_navigate_refuses_http_url() -> None:
    router, _, _ = _setup()
    resp = await _call(router, "canvas.navigate", {"agent_id": "a", "url": "https://evil.test/"})
    assert resp.result["ok"] is False
    assert "non-dashboard" in resp.result["error"].lower()


@pytest.mark.asyncio
async def test_navigate_allows_data_url() -> None:
    router, _, _ = _setup()
    resp = await _call(
        router, "canvas.navigate", {"agent_id": "a", "url": "data:text/html,<p>x</p>"}
    )
    assert resp.result["ok"] is True


@pytest.mark.asyncio
async def test_navigate_allows_about_blank() -> None:
    router, _, _ = _setup()
    resp = await _call(router, "canvas.navigate", {"agent_id": "a", "url": "about:blank"})
    assert resp.result["ok"] is True


@pytest.mark.asyncio
async def test_get_state_round_trip() -> None:
    router, _, _ = _setup()
    await _call(router, "canvas.present", {"agent_id": "a", "html": "<p>x</p>", "title": "T"})
    resp = await _call(router, "canvas.get_state", {"agent_id": "a"})
    assert resp.result["state"]["title"] == "T"
    assert resp.result["state"]["version"] == 1


@pytest.mark.asyncio
async def test_eval_round_trip_with_simulated_dashboard() -> None:
    router, _, bus = _setup()
    await _call(router, "canvas.present", {"agent_id": "a", "html": "<p>x</p>"})

    # Simulate the dashboard: subscribe, then forward eval result back.
    q = await bus.subscribe()

    async def fake_dashboard() -> None:
        evt = await q.get()
        assert evt.kind == "eval"
        await _call(
            router,
            "canvas.eval_result",
            {
                "request_id": evt.request_id,
                "ok": True,
                "value": 42,
            },
        )

    dashboard = asyncio.create_task(fake_dashboard())
    resp = await _call(
        router,
        "canvas.eval",
        {
            "agent_id": "a",
            "expression": "2+2",
            "timeout_seconds": 2.0,
        },
    )
    await dashboard
    assert resp.result == {"ok": True, "value": 42}


@pytest.mark.asyncio
async def test_eval_times_out_when_no_dashboard() -> None:
    router, _, _ = _setup()
    await _call(router, "canvas.present", {"agent_id": "a", "html": "<p>x</p>"})
    resp = await _call(
        router,
        "canvas.eval",
        {
            "agent_id": "a",
            "expression": "1",
            "timeout_seconds": 0.2,
        },
    )
    assert resp.error is None
    assert resp.result["ok"] is False
    assert "timed out" in resp.result["error"].lower()


@pytest.mark.asyncio
async def test_eval_refuses_when_no_canvas() -> None:
    router, _, _ = _setup()
    resp = await _call(router, "canvas.eval", {"agent_id": "nope", "expression": "1"})
    assert resp.error is None
    assert resp.result["ok"] is False
    assert "no visible canvas" in resp.result["error"].lower()
