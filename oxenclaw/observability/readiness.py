"""Readiness aggregation.

Liveness (`/healthz`) is trivial — if the process is up enough to
respond, it's alive. Readiness (`/readyz`) is meaningful: it asks "are my
critical subsystems all OK enough to serve traffic?" Each subsystem
contributes a `ReadinessProbe` that returns ok/degraded/down with a
short reason string.

Probes run with a tight timeout — slow probes are reported as `degraded`
rather than dragging out the response (which would defeat the point of a
readiness gate).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum


class ReadinessStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


@dataclass(frozen=True)
class ReadinessProbe:
    """One subsystem probe."""

    name: str
    check: Callable[[], Awaitable[tuple[ReadinessStatus, str]]]
    critical: bool = True
    timeout_seconds: float = 1.0


@dataclass(frozen=True)
class _ProbeResult:
    name: str
    status: ReadinessStatus
    reason: str
    critical: bool
    duration_seconds: float


@dataclass(frozen=True)
class ReadinessReport:
    """Aggregated readiness state."""

    overall: ReadinessStatus
    probes: tuple[_ProbeResult, ...]

    def is_ready(self) -> bool:
        return self.overall == ReadinessStatus.OK

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.overall.value,
            "probes": [
                {
                    "name": p.name,
                    "status": p.status.value,
                    "reason": p.reason,
                    "critical": p.critical,
                    "duration_seconds": round(p.duration_seconds, 4),
                }
                for p in self.probes
            ],
        }


class ReadinessChecker:
    """Holds a set of probes and aggregates them on demand."""

    def __init__(self) -> None:
        self._probes: list[ReadinessProbe] = []

    def register(self, probe: ReadinessProbe) -> None:
        self._probes.append(probe)

    def register_check(
        self,
        name: str,
        check: Callable[[], Awaitable[tuple[ReadinessStatus, str]]],
        *,
        critical: bool = True,
        timeout_seconds: float = 1.0,
    ) -> None:
        self.register(
            ReadinessProbe(
                name=name,
                check=check,
                critical=critical,
                timeout_seconds=timeout_seconds,
            )
        )

    @property
    def probe_count(self) -> int:
        return len(self._probes)

    async def evaluate(self) -> ReadinessReport:
        if not self._probes:
            return ReadinessReport(overall=ReadinessStatus.OK, probes=())
        results = await asyncio.gather(
            *(self._run_probe(p) for p in self._probes),
            return_exceptions=False,
        )
        overall = ReadinessStatus.OK
        for r in results:
            if r.status == ReadinessStatus.DOWN and r.critical:
                overall = ReadinessStatus.DOWN
                break
            if (
                r.status == ReadinessStatus.DOWN
                and not r.critical
                and overall == ReadinessStatus.OK
            ) or (r.status == ReadinessStatus.DEGRADED and overall == ReadinessStatus.OK):
                overall = ReadinessStatus.DEGRADED
        return ReadinessReport(overall=overall, probes=tuple(results))

    async def _run_probe(self, probe: ReadinessProbe) -> _ProbeResult:
        start = time.monotonic()
        try:
            status, reason = await asyncio.wait_for(probe.check(), timeout=probe.timeout_seconds)
        except TimeoutError:
            return _ProbeResult(
                name=probe.name,
                status=ReadinessStatus.DEGRADED,
                reason=f"timeout after {probe.timeout_seconds}s",
                critical=probe.critical,
                duration_seconds=time.monotonic() - start,
            )
        except Exception as exc:
            return _ProbeResult(
                name=probe.name,
                status=ReadinessStatus.DOWN,
                reason=f"raised: {exc.__class__.__name__}: {exc}",
                critical=probe.critical,
                duration_seconds=time.monotonic() - start,
            )
        return _ProbeResult(
            name=probe.name,
            status=status,
            reason=reason,
            critical=probe.critical,
            duration_seconds=time.monotonic() - start,
        )
