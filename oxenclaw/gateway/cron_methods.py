"""cron.* JSON-RPC methods bound to a CronScheduler."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from oxenclaw.cron import CronScheduler, NewCronJob
from oxenclaw.gateway.router import Router

if TYPE_CHECKING:
    from oxenclaw.cron.run_log import CronRunStore


class _IdParam(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str


class _ToggleParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    enabled: bool


class _RunsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str | None = None
    status: list[str] | None = None
    delivery: list[str] | None = None
    query: str | None = None
    limit: int = Field(50, ge=1, le=200)
    offset: int = Field(0, ge=0)
    sort_dir: str = "desc"


class _RunStatusParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str


class _UpdateParams(BaseModel):
    """Update mutable fields on an existing CronJob. At least one field required."""

    model_config = ConfigDict(extra="forbid")
    id: str
    schedule: str | None = None
    prompt: str | None = None
    agent_id: str | None = None
    channel: str | None = None
    account_id: str | None = None
    chat_id: str | None = None
    thread_id: str | None = None
    description: str | None = None
    enabled: bool | None = None
    start_date: str | None = None
    end_date: str | None = None


def _next_run_at(scheduler: CronScheduler, job_id: str) -> float | None:
    """Pull APScheduler's next-run timestamp, or None when the job isn't currently scheduled."""
    from apscheduler.jobstores.base import JobLookupError

    try:
        ap_job = scheduler._scheduler.get_job(job_id)
    except JobLookupError:
        return None
    if ap_job is None or ap_job.next_run_time is None:
        return None
    return ap_job.next_run_time.timestamp()


def register_cron_methods(
    router: Router,
    scheduler: CronScheduler,
    run_store: CronRunStore | None = None,
) -> None:
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

    # ------------------------------------------------------------------
    # New RPCs
    # ------------------------------------------------------------------

    @router.method("cron.runs", _RunsParams)
    async def _runs(p: _RunsParams) -> dict:  # type: ignore[type-arg]
        if run_store is None:
            return {"runs": [], "total": 0, "has_more": False}
        runs = run_store.list(
            job_id=p.job_id,
            statuses=p.status,
            delivery=p.delivery,
            query=p.query,
            limit=p.limit,
            offset=p.offset,
            sort_dir=p.sort_dir,
        )
        total = run_store.total(
            job_id=p.job_id,
            statuses=p.status,
            delivery=p.delivery,
            query=p.query,
        )
        return {
            "runs": [r.model_dump() for r in runs],
            "total": total,
            "has_more": (p.offset + len(runs)) < total,
        }

    @router.method("cron.run_status", _RunStatusParams)
    async def _run_status(p: _RunStatusParams) -> dict | None:  # type: ignore[type-arg]
        if run_store is None:
            return None
        # Linear scan — store is small enough (≤100 per job) that this is fine.
        for entry in run_store.list(limit=10_000, offset=0):
            if entry.run_id == p.run_id:
                return entry.model_dump()
        return None

    @router.method("cron.update", _UpdateParams)
    async def _update(p: _UpdateParams) -> dict:  # type: ignore[type-arg]
        job = scheduler.get(p.id)
        if job is None:
            return {"updated": False, "error": "job not found"}

        # Validate that at least one field is being changed.
        changes: dict[str, object] = {}
        for field_name in (
            "schedule",
            "prompt",
            "agent_id",
            "channel",
            "account_id",
            "chat_id",
            "thread_id",
            "description",
            "enabled",
            "start_date",
            "end_date",
        ):
            val = getattr(p, field_name, None)
            if val is not None:
                # Allow clearing date fields by sending an empty string.
                if field_name in ("start_date", "end_date") and val == "":
                    changes[field_name] = None
                else:
                    changes[field_name] = val

        if not changes:
            return {"updated": False, "error": "at least one field must be set"}

        # Validate cron schedule if provided.
        if "schedule" in changes:
            from oxenclaw.cron.models import _validate_cron

            try:
                _validate_cron(str(changes["schedule"]))
            except Exception as exc:
                return {"updated": False, "error": f"invalid schedule: {exc}"}

        # Validate date fields if provided.
        for date_field in ("start_date", "end_date"):
            if date_field in changes and changes[date_field] is not None:
                from oxenclaw.cron.models import _validate_date

                try:
                    _validate_date(str(changes[date_field]))
                except Exception as exc:
                    return {"updated": False, "error": f"invalid {date_field}: {exc}"}

        updated_job = job.model_copy(update=changes)
        scheduler._store.replace(updated_job)
        scheduler._store.save()

        # Re-sync APScheduler if the scheduler is running.
        if scheduler._started:
            if updated_job.enabled:
                scheduler._add_to_scheduler(updated_job)
            else:
                scheduler._remove_from_scheduler(updated_job.id)

        return {"updated": True, "job": updated_job.model_dump()}
