"""Cron scheduler driving the agent dispatcher.

Owns an APScheduler `AsyncIOScheduler` and a `CronJobStore`. On start,
registers every enabled job. Each fire builds an InboundEnvelope via
`build_trigger_envelope` and calls `Dispatcher.dispatch()`.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from sampyclaw.cron.models import CronJob, NewCronJob
from sampyclaw.cron.store import CronJobStore
from sampyclaw.cron.trigger import build_trigger_envelope
from sampyclaw.plugin_sdk.runtime_env import get_logger

if TYPE_CHECKING:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from sampyclaw.agents.dispatch import Dispatcher

logger = get_logger("cron.scheduler")

# Allow up to this many seconds late before treating a fire as missed.
# Combined with `coalesce=True`, a backlog after downtime collapses into one
# fire instead of either silently dropping or stampeding.
DEFAULT_MISFIRE_GRACE_SECONDS = 5 * 60


class CronScheduler:
    def __init__(
        self,
        *,
        store: CronJobStore,
        dispatcher: Dispatcher,
        timezone: str | None = None,
        misfire_grace_seconds: int = DEFAULT_MISFIRE_GRACE_SECONDS,
    ) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        self._store = store
        self._dispatcher = dispatcher
        # Explicit timezone — APScheduler defaults to system local, which
        # silently skews schedules across containers (UTC) vs operators (KST).
        # Pass `timezone="UTC"` (or any IANA name) to make this explicit.
        self._timezone = timezone
        self._misfire = misfire_grace_seconds
        self._scheduler: AsyncIOScheduler = AsyncIOScheduler(
            timezone=timezone if timezone else None,
        )
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        for job in self._store.list():
            if job.enabled:
                self._add_to_scheduler(job)
        self._scheduler.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._scheduler.shutdown(wait=False)
        self._started = False

    def list(self) -> list[CronJob]:
        return self._store.list()

    def get(self, job_id: str) -> CronJob | None:
        return self._store.get(job_id)

    def add(self, new: NewCronJob) -> CronJob:
        job = CronJob.from_new(new)
        self._store.add(job)
        self._store.save()
        if self._started and job.enabled:
            self._add_to_scheduler(job)
        return job

    def remove(self, job_id: str) -> bool:
        removed = self._store.remove(job_id)
        if not removed:
            return False
        self._store.save()
        self._remove_from_scheduler(job_id)
        return True

    def toggle(self, job_id: str, enabled: bool) -> CronJob | None:
        job = self._store.get(job_id)
        if job is None:
            return None
        updated = job.model_copy(update={"enabled": enabled})
        self._store.replace(updated)
        self._store.save()
        if not self._started:
            return updated
        if enabled:
            self._add_to_scheduler(updated)
        else:
            self._remove_from_scheduler(job_id)
        return updated

    async def fire_now(self, job_id: str) -> bool:
        """Synchronously invoke a job's handler. Primarily for tests and manual triggers."""
        job = self._store.get(job_id)
        if job is None:
            return False
        await self._fire(job_id)
        return True

    def _add_to_scheduler(self, job: CronJob) -> None:
        from apscheduler.triggers.cron import CronTrigger

        trigger = CronTrigger.from_crontab(
            job.schedule, timezone=self._timezone if self._timezone else None
        )
        self._scheduler.add_job(
            self._fire,
            trigger=trigger,
            id=job.id,
            args=[job.id],
            replace_existing=True,
            # `max_instances=1` blocks a slow handler from being re-entered;
            # `coalesce=True` collapses a backlog (e.g. after downtime) into
            # one fire; `misfire_grace_time` is how late we'll still run a
            # missed fire instead of dropping it silently.
            max_instances=1,
            coalesce=True,
            misfire_grace_time=self._misfire,
        )

    def _remove_from_scheduler(self, job_id: str) -> None:
        from apscheduler.jobstores.base import JobLookupError

        with contextlib.suppress(JobLookupError):
            self._scheduler.remove_job(job_id)

    async def _fire(self, job_id: str) -> None:
        job = self._store.get(job_id)
        if job is None or not job.enabled:
            return
        envelope = build_trigger_envelope(job)
        logger.info("cron fire id=%s agent=%s channel=%s", job.id, job.agent_id, job.channel)
        try:
            await self._dispatcher.dispatch(envelope)
        except Exception:
            logger.exception("cron job %s dispatch failed", job.id)
