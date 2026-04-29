"""healthcheck tool — aggregated subsystem probe.

Mirrors openclaw `skills/healthcheck`. Returns a multi-line text report
covering the subsystems the operator wired in. Missing subsystems are
silently skipped so a partial deployment still produces useful output.
"""

from __future__ import annotations

import time

from pydantic import BaseModel

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.channels.router import ChannelRouter
from oxenclaw.cron.scheduler import CronScheduler
from oxenclaw.memory.store import MemoryStore
from oxenclaw.pi.persistence import SQLiteSessionManager
from oxenclaw.pi.store_ops import db_size_bytes
from oxenclaw.security.isolation.registry import (
    available_backends,
)
from oxenclaw.tools_pkg._desc import hermes_desc


class _HealthArgs(BaseModel):
    pass


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n = int(n / 1024)
    return f"{n} TiB"


def healthcheck_tool(
    *,
    channels: ChannelRouter | None = None,
    cron: CronScheduler | None = None,
    sessions: SQLiteSessionManager | None = None,
    memory: MemoryStore | None = None,
) -> Tool:
    async def _h(_: _HealthArgs) -> str:
        lines: list[str] = [f"== healthcheck @ {int(time.time())} =="]

        if channels is not None:
            grouped = channels.channels_by_id()
            total = sum(len(v) for v in grouped.values())
            lines.append(f"channels: {total} bindings across {len(grouped)} kinds")
            for k, v in sorted(grouped.items()):
                lines.append(f"  {k}: {', '.join(v)}")
        else:
            lines.append("channels: (not wired)")

        if cron is not None:
            jobs = cron.list()
            enabled = sum(1 for j in jobs if j.enabled)
            lines.append(
                f"cron: {len(jobs)} jobs ({enabled} enabled, {len(jobs) - enabled} disabled)"
            )
        else:
            lines.append("cron: (not wired)")

        if sessions is not None:
            rows = await sessions.list()
            size = db_size_bytes(sessions._path)  # type: ignore[attr-defined]
            lines.append(f"sessions: {len(rows)} rows, store size {_fmt_bytes(size)}")
        else:
            lines.append("sessions: (not wired)")

        if memory is not None:
            f_count = memory.count_files()
            c_count = memory.count_chunks()
            lines.append(f"memory: {f_count} files, {c_count} indexed chunks")
        else:
            lines.append("memory: (not wired)")

        # Always probe isolation backends.
        try:
            avail = await available_backends()
            order = ["container", "bwrap", "subprocess", "inprocess"]
            strongest = next((n for n in order if n in avail), "inprocess")
            lines.append(f"isolation: available={','.join(avail)} strongest={strongest}")
        except Exception as exc:  # pragma: no cover
            lines.append(f"isolation: (probe failed: {exc})")

        return "\n".join(lines)

    return FunctionTool(
        name="healthcheck",
        description=hermes_desc(
            "Aggregate gateway / runtime health: channels, cron jobs, "
            "session store size, memory index counts, isolation backends.",
            when_use=[
                "the user asks 'is everything OK' / 'system status'",
                "you want a quick sanity probe before kicking off work",
            ],
            when_skip=[
                "the user asks about a specific subsystem (call its tool)",
                "you need actionable metrics — this is a coarse probe",
            ],
            alternatives={
                "session_logs": "agent session diagnostics",
                "wiki": "long-form docs / runbooks",
            },
        ),
        input_model=_HealthArgs,
        handler=_h,
    )


__all__ = ["healthcheck_tool"]
