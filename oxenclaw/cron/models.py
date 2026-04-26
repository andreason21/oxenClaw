"""Cron job data models.

A `CronJob` pairs a cron expression with a synthetic inbound-message
template — when the expression fires, the scheduler builds an
`InboundEnvelope` and feeds it to the agent `Dispatcher` as if a user
had sent the `prompt` on the configured channel/target.
"""

from __future__ import annotations

from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _new_id() -> str:
    return uuid4().hex


def _validate_cron(schedule: str) -> str:
    # APScheduler's CronTrigger.from_crontab is the source of truth.
    from apscheduler.triggers.cron import CronTrigger

    CronTrigger.from_crontab(schedule)
    return schedule


class NewCronJob(BaseModel):
    """Inbound shape for `cron.create` — no `id`, user supplies the rest."""

    model_config = ConfigDict(extra="forbid")

    schedule: str = Field(..., description="Standard 5-field crontab expression.")
    agent_id: str
    channel: str
    account_id: str
    chat_id: str
    thread_id: str | None = None
    prompt: str
    description: str | None = None
    enabled: bool = True

    @field_validator("schedule")
    @classmethod
    def _check_schedule(cls, v: str) -> str:
        return _validate_cron(v)


class CronJob(NewCronJob):
    """Persisted cron job. Adds the stable id assigned by the store."""

    id: str = Field(default_factory=_new_id)

    @classmethod
    def from_new(cls, new: NewCronJob, *, id: str | None = None) -> CronJob:
        return cls(id=id or _new_id(), **new.model_dump())
