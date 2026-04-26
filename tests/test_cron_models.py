"""Tests for CronJob/NewCronJob validation (especially the cron expression check)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from oxenclaw.cron.models import CronJob, NewCronJob


def test_new_cron_job_accepts_valid_expression() -> None:
    NewCronJob(
        schedule="0 9 * * *",
        agent_id="a",
        channel="telegram",
        account_id="main",
        chat_id="42",
        prompt="morning summary",
    )


def test_new_cron_job_rejects_malformed_expression() -> None:
    with pytest.raises(ValidationError):
        NewCronJob(
            schedule="not a cron",
            agent_id="a",
            channel="telegram",
            account_id="main",
            chat_id="42",
            prompt="x",
        )


def test_cron_job_assigns_stable_id() -> None:
    new = NewCronJob(
        schedule="*/5 * * * *",
        agent_id="a",
        channel="telegram",
        account_id="main",
        chat_id="1",
        prompt="x",
    )
    job = CronJob.from_new(new)
    assert job.id
    assert CronJob.from_new(new).id != job.id  # fresh ids


def test_cron_job_preserves_supplied_id() -> None:
    new = NewCronJob(
        schedule="* * * * *",
        agent_id="a",
        channel="t",
        account_id="m",
        chat_id="1",
        prompt="x",
    )
    job = CronJob.from_new(new, id="fixed-id")
    assert job.id == "fixed-id"


def test_cron_job_defaults_enabled_true() -> None:
    job = CronJob(
        schedule="* * * * *",
        agent_id="a",
        channel="t",
        account_id="m",
        chat_id="1",
        prompt="x",
    )
    assert job.enabled is True
