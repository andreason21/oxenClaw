"""Tests for CronScheduler: add/toggle/remove/fire_now behaviour.

We don't spin up the real APScheduler clock — instead tests use `fire_now`
so we can verify the dispatch path deterministically. APScheduler integration
is sanity-checked via start()/stop() not raising.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from sampyclaw.agents.dispatch import Dispatcher
from sampyclaw.agents.echo import EchoAgent
from sampyclaw.agents.registry import AgentRegistry
from sampyclaw.cron.models import NewCronJob
from sampyclaw.cron.scheduler import CronScheduler
from sampyclaw.cron.store import CronJobStore
from sampyclaw.plugin_sdk.channel_contract import SendResult
from sampyclaw.plugin_sdk.config_schema import (
    AgentChannelRouting,
    AgentConfig,
    RootConfig,
)


def _dispatcher_with_echo(send):  # type: ignore[no-untyped-def]
    agents = AgentRegistry()
    agents.register(EchoAgent(agent_id="assistant"))
    config = RootConfig(
        agents={
            "assistant": AgentConfig(
                id="assistant",
                channels={"telegram": AgentChannelRouting(allow_from=[])},
            )
        }
    )
    return Dispatcher(agents=agents, config=config, send=send)


def _new_job(prompt: str = "ping") -> NewCronJob:
    return NewCronJob(
        schedule="*/5 * * * *",
        agent_id="assistant",
        channel="telegram",
        account_id="main",
        chat_id="42",
        prompt=prompt,
    )


async def test_add_persists_and_fire_routes_to_agent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sent = []

    async def _send(params):  # type: ignore[no-untyped-def]
        sent.append(params)
        return SendResult(message_id="m1", timestamp=0.0)

    store = CronJobStore(path=tmp_path / "c.json")
    dispatcher = _dispatcher_with_echo(_send)
    scheduler = CronScheduler(store=store, dispatcher=dispatcher)
    job = scheduler.add(_new_job("morning"))

    assert scheduler.get(job.id) is not None
    assert (tmp_path / "c.json").exists()

    await scheduler.fire_now(job.id)
    assert len(sent) == 1
    assert sent[0].text == "echo: morning"


async def test_remove_drops_from_store(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = CronJobStore(path=tmp_path / "c.json")
    scheduler = CronScheduler(store=store, dispatcher=_dispatcher_with_echo(AsyncMock()))
    job = scheduler.add(_new_job())
    assert scheduler.remove(job.id) is True
    assert scheduler.remove(job.id) is False
    assert scheduler.get(job.id) is None


async def test_toggle_updates_enabled_flag(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = CronJobStore(path=tmp_path / "c.json")
    scheduler = CronScheduler(store=store, dispatcher=_dispatcher_with_echo(AsyncMock()))
    job = scheduler.add(_new_job())
    updated = scheduler.toggle(job.id, False)
    assert updated is not None and updated.enabled is False
    assert scheduler.get(job.id).enabled is False


async def test_toggle_missing_returns_none(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = CronJobStore(path=tmp_path / "c.json")
    scheduler = CronScheduler(store=store, dispatcher=_dispatcher_with_echo(AsyncMock()))
    assert scheduler.toggle("nope", False) is None


async def test_disabled_job_does_not_fire(tmp_path) -> None:  # type: ignore[no-untyped-def]
    sent = []

    async def _send(p):  # type: ignore[no-untyped-def]
        sent.append(p)
        return SendResult(message_id="m", timestamp=0.0)

    store = CronJobStore(path=tmp_path / "c.json")
    scheduler = CronScheduler(store=store, dispatcher=_dispatcher_with_echo(_send))
    job = scheduler.add(_new_job())
    scheduler.toggle(job.id, False)
    await scheduler.fire_now(job.id)
    assert sent == []


async def test_fire_now_missing_returns_false(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = CronJobStore(path=tmp_path / "c.json")
    scheduler = CronScheduler(store=store, dispatcher=_dispatcher_with_echo(AsyncMock()))
    assert await scheduler.fire_now("nope") is False


async def test_start_and_stop_are_idempotent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = CronJobStore(path=tmp_path / "c.json")
    scheduler = CronScheduler(store=store, dispatcher=_dispatcher_with_echo(AsyncMock()))
    scheduler.add(_new_job())
    scheduler.start()
    scheduler.start()  # second call is no-op, must not raise
    scheduler.stop()
    scheduler.stop()


async def test_dispatch_errors_are_swallowed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    async def _boom(_):  # type: ignore[no-untyped-def]
        raise RuntimeError("downstream broke")

    store = CronJobStore(path=tmp_path / "c.json")
    scheduler = CronScheduler(store=store, dispatcher=_dispatcher_with_echo(_boom))
    job = scheduler.add(_new_job())
    # Must not raise.
    await scheduler.fire_now(job.id)
