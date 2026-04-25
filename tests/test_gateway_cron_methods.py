"""Tests for cron.* gateway RPCs (list/create/remove/toggle/fire)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from sampyclaw.agents.dispatch import Dispatcher
from sampyclaw.agents.echo import EchoAgent
from sampyclaw.agents.registry import AgentRegistry
from sampyclaw.cron.scheduler import CronScheduler
from sampyclaw.cron.store import CronJobStore
from sampyclaw.gateway.cron_methods import register_cron_methods
from sampyclaw.gateway.router import Router
from sampyclaw.plugin_sdk.channel_contract import SendResult
from sampyclaw.plugin_sdk.config_schema import (
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
                    channels={"telegram": AgentChannelRouting(allow_from=[])},
                )
            }
        ),
        send=AsyncMock(return_value=SendResult(message_id="m", timestamp=0.0)),
    )


def _setup(tmp_path):  # type: ignore[no-untyped-def]
    store = CronJobStore(path=tmp_path / "cron.json")
    scheduler = CronScheduler(store=store, dispatcher=_dispatcher())
    router = Router()
    register_cron_methods(router, scheduler)
    return router, scheduler


def _create_params(**overrides):  # type: ignore[no-untyped-def]
    base = {
        "schedule": "*/5 * * * *",
        "agent_id": "assistant",
        "channel": "telegram",
        "account_id": "main",
        "chat_id": "42",
        "prompt": "ping",
    }
    base.update(overrides)
    return base


async def test_create_persists_and_returns_id(tmp_path) -> None:  # type: ignore[no-untyped-def]
    router, scheduler = _setup(tmp_path)
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "cron.create", "params": _create_params()}
    )
    assert resp.error is None
    assert "id" in resp.result
    assert len(scheduler.list()) == 1


async def test_list_returns_all_jobs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    router, _ = _setup(tmp_path)
    for i in range(2):
        await router.dispatch(
            {"jsonrpc": "2.0", "id": 1, "method": "cron.create", "params": _create_params(prompt=f"p{i}")}
        )
    resp = await router.dispatch({"jsonrpc": "2.0", "id": 1, "method": "cron.list"})
    assert len(resp.result) == 2


async def test_remove_returns_bool(tmp_path) -> None:  # type: ignore[no-untyped-def]
    router, scheduler = _setup(tmp_path)
    job = scheduler.add_from_create_result = None  # noqa: we'll use scheduler directly
    create_resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "cron.create", "params": _create_params()}
    )
    job_id = create_resp.result["id"]
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "cron.remove", "params": {"id": job_id}}
    )
    assert resp.result == {"removed": True}
    resp2 = await router.dispatch(
        {"jsonrpc": "2.0", "id": 3, "method": "cron.remove", "params": {"id": job_id}}
    )
    assert resp2.result == {"removed": False}


async def test_toggle_updates_enabled(tmp_path) -> None:  # type: ignore[no-untyped-def]
    router, scheduler = _setup(tmp_path)
    create_resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "cron.create", "params": _create_params()}
    )
    jid = create_resp.result["id"]
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "cron.toggle",
            "params": {"id": jid, "enabled": False},
        }
    )
    assert resp.result["toggled"] is True
    assert scheduler.get(jid).enabled is False


async def test_fire_dispatches_to_agent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    router, scheduler = _setup(tmp_path)
    create_resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "cron.create", "params": _create_params(prompt="fire-test")}
    )
    jid = create_resp.result["id"]
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "cron.fire", "params": {"id": jid}}
    )
    assert resp.result == {"fired": True}


async def test_create_rejects_bad_schedule(tmp_path) -> None:  # type: ignore[no-untyped-def]
    router, _ = _setup(tmp_path)
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "cron.create",
            "params": _create_params(schedule="not a cron"),
        }
    )
    assert resp.error is not None


async def test_list_includes_next_run_at_field(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """`cron.list` returns each job with next_run_at — None if scheduler not started, else a float timestamp."""
    router, scheduler = _setup(tmp_path)
    create_resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "cron.create", "params": _create_params()}
    )
    job_id = create_resp.result["id"]
    resp = await router.dispatch({"jsonrpc": "2.0", "id": 2, "method": "cron.list"})
    rows = resp.result
    assert len(rows) == 1
    assert rows[0]["id"] == job_id
    assert "next_run_at" in rows[0]
    # Scheduler hasn't been .start()ed → no apscheduler job yet → null
    assert rows[0]["next_run_at"] is None

    scheduler.start()
    try:
        resp2 = await router.dispatch(
            {"jsonrpc": "2.0", "id": 3, "method": "cron.list"}
        )
        next_run = resp2.result[0]["next_run_at"]
        assert next_run is None or isinstance(next_run, (int, float))
    finally:
        scheduler.stop()


async def test_next_run_for_unknown_job(tmp_path) -> None:  # type: ignore[no-untyped-def]
    router, _ = _setup(tmp_path)
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "cron.next_run", "params": {"id": "ghost"}}
    )
    assert resp.result == {"found": False, "next_run_at": None}


async def test_next_run_existing_unscheduled(tmp_path) -> None:  # type: ignore[no-untyped-def]
    router, _ = _setup(tmp_path)
    create = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "cron.create", "params": _create_params()}
    )
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "cron.next_run",
            "params": {"id": create.result["id"]},
        }
    )
    # Found in store, but never loaded into scheduler since scheduler.start() wasn't called.
    assert resp.result["found"] is True
    assert resp.result["scheduled"] is False
    assert resp.result["next_run_at"] is None
