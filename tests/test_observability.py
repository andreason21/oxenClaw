"""Tests for sampyclaw.observability."""

from __future__ import annotations

import asyncio

import pytest

from sampyclaw.observability import (
    Counter,
    Gauge,
    Histogram,
    Metrics,
    ReadinessChecker,
    ReadinessStatus,
    render_prometheus,
)

# ----------------------------------------------------------------- counters


def test_counter_increments_and_renders():
    c = Counter(
        name="x_total",
        help="An example counter.",
        label_names=("kind",),
    )
    c.inc(labels={"kind": "a"})
    c.inc(2.5, labels={"kind": "a"})
    c.inc(labels={"kind": "b"})
    assert c.get({"kind": "a"}) == 3.5
    assert c.get({"kind": "b"}) == 1.0

    rendered = "\n".join(c.render_lines())
    assert "TYPE x_total counter" in rendered
    assert 'x_total{kind="a"} 3.5' in rendered
    assert 'x_total{kind="b"} 1.0' in rendered


def test_counter_rejects_negative():
    c = Counter("y", "y", ())
    with pytest.raises(ValueError):
        c.inc(-1)


# ----------------------------------------------------------------- gauges


def test_gauge_set_inc_dec():
    g = Gauge("g", "g")
    g.set(5)
    g.inc(2)
    g.dec(1)
    assert g.get() == 6.0


# ----------------------------------------------------------------- histograms


def test_histogram_observes_and_renders():
    h = Histogram(
        "h_seconds",
        "h",
        buckets=(0.1, 1.0, float("inf")),
    )
    for v in (0.05, 0.5, 1.5, 2.0):
        h.observe(v)
    rendered = "\n".join(h.render_lines())
    # le=0.1 sees only the 0.05 sample → count 1
    assert 'h_seconds_bucket{le="0.1"} 1' in rendered
    # le=1.0 sees 0.05, 0.5 → 2
    assert 'h_seconds_bucket{le="1.0"} 2' in rendered
    # le=+Inf sees all 4
    assert 'h_seconds_bucket{le="+Inf"} 4' in rendered
    assert "h_seconds_sum" in rendered
    assert "h_seconds_count" in rendered


# ----------------------------------------------------------------- metrics


def test_metrics_registry_exposes_all_metrics():
    m = Metrics()
    names = {metric.name for metric in m.all_metrics()}
    expected = {
        "sampyclaw_ws_connections_active",
        "sampyclaw_ws_rpc_total",
        "sampyclaw_ws_rpc_duration_seconds",
        "sampyclaw_channel_inbound_total",
        "sampyclaw_channel_outbound_total",
        "sampyclaw_agent_turns_total",
        "sampyclaw_tool_calls_total",
        "sampyclaw_mcp_tool_calls_total",
        "sampyclaw_cron_jobs_active",
        "sampyclaw_approvals_pending",
    }
    assert expected.issubset(names)


def test_render_prometheus_produces_lines():
    m = Metrics()
    m.ws_rpc_total.inc(labels={"method": "chat.send"})
    rendered = render_prometheus(m)
    assert "TYPE sampyclaw_ws_rpc_total counter" in rendered
    assert 'sampyclaw_ws_rpc_total{method="chat.send"} 1.0' in rendered


def test_label_value_escapes_quotes_and_newlines():
    c = Counter("e", "e", ("k",))
    c.inc(labels={"k": 'a"b\nc'})
    rendered = "\n".join(c.render_lines())
    assert 'k="a\\"b\\nc"' in rendered


# ----------------------------------------------------------------- readiness


@pytest.mark.asyncio
async def test_readiness_empty_checker_returns_ok():
    checker = ReadinessChecker()
    report = await checker.evaluate()
    assert report.is_ready()
    assert report.probes == ()


@pytest.mark.asyncio
async def test_readiness_aggregates_probe_statuses():
    checker = ReadinessChecker()

    async def ok_probe():
        return ReadinessStatus.OK, "fine"

    async def degraded_probe():
        return ReadinessStatus.DEGRADED, "slow"

    async def down_probe():
        return ReadinessStatus.DOWN, "broken"

    checker.register_check("a", ok_probe)
    checker.register_check("b", degraded_probe)
    checker.register_check("c", down_probe, critical=False)

    report = await checker.evaluate()
    # one critical probe down → critical aggregate? No — c is non-critical,
    # so the worst we have is DOWN-non-critical + DEGRADED-critical → DEGRADED.
    assert report.overall == ReadinessStatus.DEGRADED


@pytest.mark.asyncio
async def test_readiness_critical_down_makes_overall_down():
    checker = ReadinessChecker()

    async def down():
        return ReadinessStatus.DOWN, "kaboom"

    checker.register_check("db", down, critical=True)
    report = await checker.evaluate()
    assert report.overall == ReadinessStatus.DOWN
    assert not report.is_ready()


@pytest.mark.asyncio
async def test_readiness_timeout_is_degraded():
    checker = ReadinessChecker()

    async def slow():
        await asyncio.sleep(5)
        return ReadinessStatus.OK, "eventually"

    checker.register_check("slow", slow, timeout_seconds=0.05)
    report = await checker.evaluate()
    assert report.overall == ReadinessStatus.DEGRADED
    assert "timeout" in report.probes[0].reason


@pytest.mark.asyncio
async def test_readiness_exception_is_down():
    checker = ReadinessChecker()

    async def boom():
        raise RuntimeError("oops")

    checker.register_check("boom", boom, critical=True)
    report = await checker.evaluate()
    assert report.overall == ReadinessStatus.DOWN
    assert "oops" in report.probes[0].reason


@pytest.mark.asyncio
async def test_readiness_to_dict_serializable():
    import json

    checker = ReadinessChecker()

    async def ok():
        return ReadinessStatus.OK, "fine"

    checker.register_check("a", ok)
    report = await checker.evaluate()
    out = report.to_dict()
    json.dumps(out)  # must not raise
    assert out["status"] == "ok"
    assert out["probes"][0]["name"] == "a"
