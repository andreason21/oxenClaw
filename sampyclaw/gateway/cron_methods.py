"""cron.* JSON-RPC methods bound to a CronScheduler."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from sampyclaw.cron import CronScheduler, NewCronJob
from sampyclaw.gateway.router import Router


class _IdParam(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str


class _ToggleParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    enabled: bool


def _next_run_at(scheduler: CronScheduler, job_id: str) -> float | None:
    """Pull APScheduler's next-run timestamp, or None when the job isn't currently scheduled."""
    from apscheduler.jobstores.base import JobLookupError

    try:
        ap_job = scheduler._scheduler.get_job(job_id)  # noqa: SLF001
    except JobLookupError:
        return None
    if ap_job is None or ap_job.next_run_time is None:
        return None
    return ap_job.next_run_time.timestamp()


def register_cron_methods(router: Router, scheduler: CronScheduler) -> None:
    @router.method("cron.list")
    async def _list(_: dict) -> list[dict]:  # type: ignore[type-arg]
        out: list[dict] = []  # type: ignore[type-arg]
        for j in scheduler.list():
            row = j.model_dump()
            row["next_run_at"] = _next_run_at(scheduler, j.id)
            out.append(row)
        return out

    @router.method("cron.next_run", _IdParam)
    async def _next_run(p: _IdParam) -> dict:  # type: ignore[type-arg]
        job = scheduler.get(p.id)
        if job is None:
            return {"found": False, "next_run_at": None}
        ts = _next_run_at(scheduler, p.id)
        return {"found": True, "scheduled": ts is not None, "next_run_at": ts}

    @router.method("cron.create", NewCronJob)
    async def _create(p: NewCronJob) -> dict:  # type: ignore[type-arg]
        job = scheduler.add(p)
        return {"id": job.id}

    @router.method("cron.remove", _IdParam)
    async def _remove(p: _IdParam) -> dict:  # type: ignore[type-arg]
        return {"removed": scheduler.remove(p.id)}

    @router.method("cron.toggle", _ToggleParams)
    async def _toggle(p: _ToggleParams) -> dict:  # type: ignore[type-arg]
        updated = scheduler.toggle(p.id, p.enabled)
        return {"toggled": updated is not None, "enabled": p.enabled if updated else None}

    @router.method("cron.fire", _IdParam)
    async def _fire(p: _IdParam) -> dict:  # type: ignore[type-arg]
        return {"fired": await scheduler.fire_now(p.id)}
