"""Tests for `usage.session` / `usage.totals` RPCs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.gateway.router import Router
from oxenclaw.gateway.usage_methods import register_usage_methods


def _paths(tmp: Path) -> OxenclawPaths:
    p = OxenclawPaths(home=tmp)
    p.ensure_home()
    return p


def _write_usage(paths: OxenclawPaths, agent: str, key: str, **fields: float) -> None:
    f = paths.usage_file(agent, key)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(fields), encoding="utf-8")


@pytest.fixture()
def router(tmp_path: Path) -> tuple[Router, OxenclawPaths]:
    paths = _paths(tmp_path)
    r = Router()
    register_usage_methods(r, paths=paths)
    return r, paths


async def test_usage_session_returns_zeros_for_missing_file(router) -> None:  # type: ignore[no-untyped-def]
    r, _paths = router
    resp = await r.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "usage.session",
            "params": {"agent_id": "assistant", "session_key": "dashboard:main:demo"},
        }
    )
    assert resp.error is None
    assert resp.result == {
        "turns": 0,
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_create": 0,
        "cost_usd": 0.0,
        "hit_rate": 0.0,
    }


async def test_usage_session_reads_persisted_file(router) -> None:  # type: ignore[no-untyped-def]
    r, paths = router
    _write_usage(
        paths,
        "assistant",
        "dashboard:main:demo",
        turns=3,
        input=120,
        output=80,
        cache_read=40,
        cache_create=20,
        cost_usd=0.0123,
        hit_rate=0.25,
    )
    resp = await r.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "usage.session",
            "params": {"agent_id": "assistant", "session_key": "dashboard:main:demo"},
        }
    )
    assert resp.error is None
    assert resp.result["turns"] == 3
    assert resp.result["input"] == 120
    assert resp.result["output"] == 80
    assert resp.result["cost_usd"] == 0.0123


async def test_usage_totals_aggregates_across_sessions_and_agents(router) -> None:  # type: ignore[no-untyped-def]
    r, paths = router
    _write_usage(paths, "assistant", "k1", turns=2, input=100, output=50, cost_usd=0.01)
    _write_usage(paths, "assistant", "k2", turns=1, input=50, output=20, cost_usd=0.005)
    _write_usage(paths, "coder", "k3", turns=4, input=200, output=80, cost_usd=0.02,
                 cache_read=60)
    resp = await r.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "usage.totals", "params": {}}
    )
    assert resp.error is None
    total = resp.result["total"]
    assert total["turns"] == 7
    assert total["input"] == 350
    assert total["output"] == 150
    # cost rounding tolerant comparison
    assert abs(total["cost_usd"] - 0.035) < 1e-6
    # hit_rate = cache_read / (cache_read + input) = 60 / (60 + 350)
    assert abs(total["hit_rate"] - round(60 / (60 + 350), 4)) < 1e-6
    # per_agent rollups
    by_agent = {row["agent_id"]: row for row in resp.result["per_agent"]}
    assert by_agent["assistant"]["turns"] == 3
    assert by_agent["coder"]["turns"] == 4


async def test_usage_totals_filters_by_agent_id(router) -> None:  # type: ignore[no-untyped-def]
    r, paths = router
    _write_usage(paths, "assistant", "k1", turns=2, input=100)
    _write_usage(paths, "coder", "k2", turns=4, input=200)
    resp = await r.dispatch(
        {
            "jsonrpc": "2.0", "id": 1, "method": "usage.totals",
            "params": {"agent_id": "coder"},
        }
    )
    assert resp.error is None
    assert resp.result["total"]["turns"] == 4
    assert resp.result["total"]["input"] == 200
    assert [row["agent_id"] for row in resp.result["per_agent"]] == ["coder"]
