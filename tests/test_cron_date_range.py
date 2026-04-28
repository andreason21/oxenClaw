"""Tests for the start_date/end_date fields on CronJob.

Covers model validation, scheduler trigger wiring, and the cron.update RPC
clearing dates via empty-string sentinel.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from oxenclaw.agents.dispatch import Dispatcher
from oxenclaw.agents.echo import EchoAgent
from oxenclaw.agents.registry import AgentRegistry
from oxenclaw.cron.models import NewCronJob
from oxenclaw.cron.scheduler import CronScheduler
from oxenclaw.cron.store import CronJobStore
from oxenclaw.gateway.cron_methods import register_cron_methods
from oxenclaw.gateway.router import Router
from oxenclaw.plugin_sdk.channel_contract import SendResult
from oxenclaw.plugin_sdk.config_schema import (
    AgentChannelRouting,
    AgentConfig,
    RootConfig,
)


def _dispatcher():  # type: ignore[no-untyped-def]
    agents = AgentRegistry()
    agents.register(EchoAgent(agent_id="assistant"))
    return Dispatcher(
        agents=agents,
        config=RootConfig(
            agents={
                "assistant": AgentConfig(
                    id="assistant",
                    channels={"dashboard": AgentChannelRouting(allow_from=[])},
                )
            }
        ),
        send=AsyncMock(return_value=SendResult(message_id="m", timestamp=0.0)),
    )


def _new(**over):  # type: ignore[no-untyped-def]
    base = dict(
        schedule="0 9 * * *",
        agent_id="assistant",
        channel="dashboard",
        account_id="main",
        chat_id="42",
        prompt="x",
    )
    base.update(over)
    return NewCronJob(**base)


def test_model_accepts_iso_dates() -> None:
    job = _new(start_date="2026-05-01", end_date="2026-05-31")
    assert job.start_date == "2026-05-01"
    assert job.end_date == "2026-05-31"


def test_model_accepts_iso_datetime() -> None:
    job = _new(start_date="2026-05-01T08:30:00")
    assert job.start_date == "2026-05-01T08:30:00"


def test_model_treats_empty_string_as_none() -> None:
    job = _new(start_date="", end_date="")
    assert job.start_date is None
    assert job.end_date is None


def test_model_rejects_garbage_date() -> None:
    with pytest.raises(ValidationError):
        _new(start_date="not-a-date")


async def test_scheduler_propagates_dates_to_apscheduler(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = CronJobStore(path=tmp_path / "c.json")
    scheduler = CronScheduler(store=store, dispatcher=_dispatcher())
    job = scheduler.add(_new(start_date="2026-05-01", end_date="2026-05-31"))
    scheduler.start()
    try:
        ap_job = scheduler._scheduler.get_job(job.id)
        assert ap_job is not None
        assert ap_job.trigger.start_date is not None
        assert ap_job.trigger.start_date.date().isoformat() == "2026-05-01"
        assert ap_job.trigger.end_date is not None
        assert ap_job.trigger.end_date.date().isoformat() == "2026-05-31"
    finally:
        scheduler.stop()


async def test_update_can_set_and_clear_dates(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = CronJobStore(path=tmp_path / "c.json")
    scheduler = CronScheduler(store=store, dispatcher=_dispatcher())
    router = Router()
    register_cron_methods(router, scheduler)

    create_res = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "cron.create",
            "params": dict(
                schedule="0 9 * * *",
                agent_id="assistant",
                channel="dashboard",
                account_id="main",
                chat_id="42",
                prompt="x",
            ),
        }
    )
    job_id = create_res.result["id"]

    set_res = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "cron.update",
            "params": {"id": job_id, "start_date": "2026-06-01", "end_date": "2026-06-30"},
        }
    )
    assert set_res.result["updated"] is True
    assert scheduler.get(job_id).start_date == "2026-06-01"
    assert scheduler.get(job_id).end_date == "2026-06-30"

    # Empty string clears the field.
    clr_res = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "cron.update",
            "params": {"id": job_id, "start_date": "", "end_date": ""},
        }
    )
    assert clr_res.result["updated"] is True
    assert scheduler.get(job_id).start_date is None
    assert scheduler.get(job_id).end_date is None


async def test_update_rejects_invalid_date(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = CronJobStore(path=tmp_path / "c.json")
    scheduler = CronScheduler(store=store, dispatcher=_dispatcher())
    router = Router()
    register_cron_methods(router, scheduler)

    create_res = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "cron.create",
            "params": dict(
                schedule="0 9 * * *",
                agent_id="assistant",
                channel="dashboard",
                account_id="main",
                chat_id="42",
                prompt="x",
            ),
        }
    )
    job_id = create_res.result["id"]

    res = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "cron.update",
            "params": {"id": job_id, "start_date": "not-a-date"},
        }
    )
    assert res.result["updated"] is False
    assert "invalid start_date" in res.result["error"]
