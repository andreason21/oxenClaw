"""Observability primitives — metrics + correlation context.

Kept zero-dep on purpose: a tiny in-process Prometheus-text formatter so
operators can scrape the gateway without pulling `prometheus_client`.
External SDKs can be plugged in later — `metrics.snapshot()` returns the
data in a structure that's easy to translate to OpenTelemetry / StatsD.
"""

from sampyclaw.observability.logging import (
    JsonFormatter,
    configure_logging,
    correlation_scope,
    get_context,
    new_correlation_id,
)
from sampyclaw.observability.metrics import (
    METRICS,
    Counter,
    Gauge,
    Histogram,
    Metrics,
    render_prometheus,
)
from sampyclaw.observability.readiness import (
    ReadinessChecker,
    ReadinessProbe,
    ReadinessReport,
    ReadinessStatus,
)

__all__ = [
    "Counter",
    "Gauge",
    "Histogram",
    "JsonFormatter",
    "METRICS",
    "Metrics",
    "ReadinessChecker",
    "ReadinessProbe",
    "ReadinessReport",
    "ReadinessStatus",
    "configure_logging",
    "correlation_scope",
    "get_context",
    "new_correlation_id",
    "render_prometheus",
]
