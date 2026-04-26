"""In-process metrics registry + Prometheus text-format renderer.

Three primitives — Counter, Gauge, Histogram — covering 95% of operational
metrics. Each metric supports one optional label dimension (passed as a
`{label_name: label_value}` dict). The registry is a process-wide
singleton (`METRICS`) accessed by every subsystem that wants to emit.

We deliberately don't depend on `prometheus_client`: the entire format
this module needs to produce is line-based plain text, and pulling a
40-package wheel chain for that is wasteful. If operators want native
Prometheus features later, swapping the renderer is a half-page change.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field

# Standard Prometheus quantile-friendly bucket layout (in seconds for
# duration histograms, but works for any unit-positive numeric).
DEFAULT_HISTOGRAM_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    math.inf,
)


def _label_signature(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    return tuple(sorted(labels.items()))


def _format_label_pairs(sig: tuple[tuple[str, str], ...]) -> str:
    if not sig:
        return ""
    parts = [f'{name}="{_escape_label_value(value)}"' for name, value in sig]
    return "{" + ",".join(parts) + "}"


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@dataclass
class Counter:
    """Monotonically non-decreasing — increments by `n >= 0`."""

    name: str
    help: str
    label_names: tuple[str, ...] = ()
    _values: dict[tuple[tuple[str, str], ...], float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def inc(self, n: float = 1.0, labels: dict[str, str] | None = None) -> None:
        if n < 0:
            raise ValueError("counter increment must be non-negative")
        sig = _label_signature(labels)
        with self._lock:
            self._values[sig] = self._values.get(sig, 0.0) + n

    def get(self, labels: dict[str, str] | None = None) -> float:
        sig = _label_signature(labels)
        with self._lock:
            return self._values.get(sig, 0.0)

    def render_lines(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} counter"
        with self._lock:
            snapshot = dict(self._values)
        for sig, value in snapshot.items():
            yield f"{self.name}{_format_label_pairs(sig)} {value}"


@dataclass
class Gauge:
    """Arbitrary up-or-down value (queue depth, active connections, …)."""

    name: str
    help: str
    label_names: tuple[str, ...] = ()
    _values: dict[tuple[tuple[str, str], ...], float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def set(self, value: float, labels: dict[str, str] | None = None) -> None:
        sig = _label_signature(labels)
        with self._lock:
            self._values[sig] = float(value)

    def inc(self, n: float = 1.0, labels: dict[str, str] | None = None) -> None:
        sig = _label_signature(labels)
        with self._lock:
            self._values[sig] = self._values.get(sig, 0.0) + n

    def dec(self, n: float = 1.0, labels: dict[str, str] | None = None) -> None:
        self.inc(-n, labels)

    def get(self, labels: dict[str, str] | None = None) -> float:
        sig = _label_signature(labels)
        with self._lock:
            return self._values.get(sig, 0.0)

    def render_lines(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} gauge"
        with self._lock:
            snapshot = dict(self._values)
        for sig, value in snapshot.items():
            yield f"{self.name}{_format_label_pairs(sig)} {value}"


@dataclass
class Histogram:
    """Cumulative bucketed distribution + sum + count.

    Renders the Prometheus-native `_bucket{le="..."}` / `_sum` / `_count`
    triple. Default buckets cover request-duration ranges; pass `buckets=`
    for other shapes.
    """

    name: str
    help: str
    label_names: tuple[str, ...] = ()
    buckets: tuple[float, ...] = DEFAULT_HISTOGRAM_BUCKETS
    _bucket_counts: dict[tuple[tuple[str, str], ...], list[int]] = field(default_factory=dict)
    _sum: dict[tuple[tuple[str, str], ...], float] = field(default_factory=dict)
    _count: dict[tuple[tuple[str, str], ...], int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def observe(self, value: float, labels: dict[str, str] | None = None) -> None:
        sig = _label_signature(labels)
        with self._lock:
            counts = self._bucket_counts.get(sig)
            if counts is None:
                counts = [0] * len(self.buckets)
                self._bucket_counts[sig] = counts
            for i, b in enumerate(self.buckets):
                if value <= b:
                    counts[i] += 1
            self._sum[sig] = self._sum.get(sig, 0.0) + value
            self._count[sig] = self._count.get(sig, 0) + 1

    def render_lines(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} histogram"
        with self._lock:
            sigs = list(self._bucket_counts.keys())
            buckets = list(self.buckets)
            counts_snap = {s: list(self._bucket_counts[s]) for s in sigs}
            sum_snap = dict(self._sum)
            count_snap = dict(self._count)
        for sig in sigs:
            for b, c in zip(buckets, counts_snap[sig], strict=False):
                le = "+Inf" if b == math.inf else f"{b}"
                merged = (*sig, ("le", le))
                yield f"{self.name}_bucket{_format_label_pairs(merged)} {c}"
            yield (f"{self.name}_sum{_format_label_pairs(sig)} {sum_snap.get(sig, 0.0)}")
            yield (f"{self.name}_count{_format_label_pairs(sig)} {count_snap.get(sig, 0)}")


class Metrics:
    """Process-wide registry + the metric definitions themselves.

    Adding a new metric: declare it as an attribute below; subsystems
    `from oxenclaw.observability import METRICS` and call
    `METRICS.<metric>.inc(...)`.
    """

    def __init__(self) -> None:
        # WS / RPC
        self.ws_connections_active = Gauge(
            "oxenclaw_ws_connections_active",
            "Number of WebSocket connections currently held by the gateway.",
        )
        self.ws_rpc_total = Counter(
            "oxenclaw_ws_rpc_total",
            "Total WS JSON-RPC requests received (per method).",
            label_names=("method",),
        )
        self.ws_rpc_errors_total = Counter(
            "oxenclaw_ws_rpc_errors_total",
            "Total WS JSON-RPC error responses (per method).",
            label_names=("method",),
        )
        self.ws_rpc_duration_seconds = Histogram(
            "oxenclaw_ws_rpc_duration_seconds",
            "WS JSON-RPC handler duration in seconds.",
            label_names=("method",),
        )
        # Channels
        self.channel_inbound_total = Counter(
            "oxenclaw_channel_inbound_total",
            "Total inbound channel messages received.",
            label_names=("channel",),
        )
        self.channel_outbound_total = Counter(
            "oxenclaw_channel_outbound_total",
            "Total outbound channel messages sent.",
            label_names=("channel",),
        )
        self.channel_outbound_errors_total = Counter(
            "oxenclaw_channel_outbound_errors_total",
            "Total outbound channel send errors.",
            label_names=("channel",),
        )
        # Agents
        self.agent_turns_total = Counter(
            "oxenclaw_agent_turns_total",
            "Total agent turns dispatched.",
            label_names=("agent_id", "provider"),
        )
        self.agent_turn_duration_seconds = Histogram(
            "oxenclaw_agent_turn_duration_seconds",
            "Agent turn duration in seconds (model + tools).",
            label_names=("agent_id",),
        )
        # Tools
        self.tool_calls_total = Counter(
            "oxenclaw_tool_calls_total",
            "Total tool invocations.",
            label_names=("tool",),
        )
        self.tool_call_duration_seconds = Histogram(
            "oxenclaw_tool_call_duration_seconds",
            "Tool execution time in seconds.",
            label_names=("tool",),
        )
        self.tool_call_errors_total = Counter(
            "oxenclaw_tool_call_errors_total",
            "Total tool invocations that raised an exception.",
            label_names=("tool",),
        )
        # MCP
        self.mcp_servers_connected = Gauge(
            "oxenclaw_mcp_servers_connected",
            "Number of MCP servers the gateway is currently connected to.",
        )
        self.mcp_tool_calls_total = Counter(
            "oxenclaw_mcp_tool_calls_total",
            "Total MCP tool invocations.",
            label_names=("server",),
        )
        # Cron
        self.cron_jobs_active = Gauge(
            "oxenclaw_cron_jobs_active",
            "Number of cron jobs currently scheduled.",
        )
        self.cron_jobs_fired_total = Counter(
            "oxenclaw_cron_jobs_fired_total",
            "Total cron job firings (success + error).",
        )
        self.cron_jobs_errors_total = Counter(
            "oxenclaw_cron_jobs_errors_total",
            "Total cron job firings that ended in error.",
        )
        # Approvals
        self.approvals_pending = Gauge(
            "oxenclaw_approvals_pending",
            "Number of approval requests currently awaiting a decision.",
        )
        self.approvals_resolved_total = Counter(
            "oxenclaw_approvals_resolved_total",
            "Total approval requests resolved.",
            label_names=("status",),
        )
        # Process / build info
        self.build_info = Gauge(
            "oxenclaw_build_info",
            "1 — labels carry build metadata.",
        )

    def all_metrics(self) -> list[Counter | Gauge | Histogram]:
        return [v for v in self.__dict__.values() if isinstance(v, (Counter, Gauge, Histogram))]


METRICS = Metrics()


def render_prometheus(metrics: Metrics | None = None) -> str:
    """Render the registry as Prometheus exposition format text."""
    target = metrics if metrics is not None else METRICS
    lines: list[str] = []
    for metric in target.all_metrics():
        lines.extend(metric.render_lines())
    lines.append("")  # trailing newline
    return "\n".join(lines)
