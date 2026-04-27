"""Tests for new cron gateway RPCs: cron.runs, cron.run_status, cron.update (≥5 tests)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

from oxenclaw.agents.dispatch import Dispatcher
from oxenclaw.agents.echo import EchoAgent
from oxenclaw.agents.registry import AgentRegistry
from oxenclaw.cron.models import NewCronJob
from oxenclaw.cron.run_log import CronRunEntry, CronRunStore
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


# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────

def _dispatcher() -> Dispatcher:
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


def _setup(tmp_path):  # type: ignore[no-untyped-def]
    job_store = CronJobStore(path=tmp_path / "cron.json")
    run_store = CronRunStore(tmp_path / "runs.json")
    scheduler = CronScheduler(store=job_store, dispatcher=_dispatcher(), run_store=run_store)
    router = Router()
    register_cron_methods(router, scheduler, run_store=run_store)
    return router, scheduler, run_store


def _create_params(**overrides):  # type: ignore[no-untyped-def]
    base = {
        "schedule": "*/5 * * * *",
        "agent_id": "assistant",
        "channel": "dashboard",
        "account_id": "main",
        "chat_id": "42",
        "prompt": "ping",
    }
    base.update(overrides)
    return base


def _seed_runs(run_store: CronRunStore, job_id: str, count: int = 3) -> list[CronRunEntry]:
    entries = []
    for i in range(count):
        e = CronRunEntry(job_id=job_id, started_at=float(i), status="ok", summary=f"run {i}")
        run_store.append(e)
        entries.append(e)
    return entries


# ──────────────────────────────────────────────────────────
# 1. cron.runs returns paged list
# ──────────────────────────────────────────────────────────

async def test_cron_runs_paged_list(tmp_path) -> None:
    router, scheduler, run_store = _setup(tmp_path)
    create_resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "cron.create", "params": _create_params()}
    )
    job_id = create_resp.result["id"]
    _seed_runs(run_store, job_id, count=5)

    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "cron.runs", "params": {"job_id": job_id, "limit": 3, "offset": 0}}
    )
    assert resp.error is None
    body = resp.result
    assert body["total"] == 5
    assert len(body["runs"]) == 3
    assert body["has_more"] is True


# ──────────────────────────────────────────────────────────
# 2. cron.runs status filter
# ──────────────────────────────────────────────────────────

async def test_cron_runs_status_filter(tmp_path) -> None:
    router, scheduler, run_store = _setup(tmp_path)
    run_store.append(CronRunEntry(job_id="j1", started_at=1.0, status="ok"))
    run_store.append(CronRunEntry(job_id="j1", started_at=2.0, status="error"))
    run_store.append(CronRunEntry(job_id="j1", started_at=3.0, status="ok"))

    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "cron.runs", "params": {"status": ["error"]}}
    )
    body = resp.result
    assert body["total"] == 1
    assert body["runs"][0]["status"] == "error"


# ──────────────────────────────────────────────────────────
# 3. cron.runs query substring filter
# ──────────────────────────────────────────────────────────

async def test_cron_runs_query_filter(tmp_path) -> None:
    router, scheduler, run_store = _setup(tmp_path)
    run_store.append(CronRunEntry(job_id="j1", started_at=1.0, summary="morning digest done"))
    run_store.append(CronRunEntry(job_id="j1", started_at=2.0, summary="afternoon update"))

    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "cron.runs", "params": {"query": "morning"}}
    )
    body = resp.result
    assert body["total"] == 1
    assert "morning" in body["runs"][0]["summary"]


# ──────────────────────────────────────────────────────────
# 4. cron.run_status of unknown id returns None
# ──────────────────────────────────────────────────────────

async def test_cron_run_status_unknown(tmp_path) -> None:
    router, scheduler, run_store = _setup(tmp_path)

    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "cron.run_status", "params": {"run_id": "ghost-run-id"}}
    )
    assert resp.error is None
    assert resp.result is None


# ──────────────────────────────────────────────────────────
# 5. cron.run_status returns existing entry
# ──────────────────────────────────────────────────────────

async def test_cron_run_status_existing(tmp_path) -> None:
    router, scheduler, run_store = _setup(tmp_path)
    entry = CronRunEntry(job_id="j1", started_at=time.time(), status="ok", summary="done")
    run_store.append(entry)

    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "cron.run_status", "params": {"run_id": entry.run_id}}
    )
    assert resp.error is None
    assert resp.result is not None
    assert resp.result["run_id"] == entry.run_id
    assert resp.result["status"] == "ok"


# ──────────────────────────────────────────────────────────
# 6. cron.update changes a field and persists
# ──────────────────────────────────────────────────────────

async def test_cron_update_prompt(tmp_path) -> None:
    router, scheduler, run_store = _setup(tmp_path)
    create_resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "cron.create", "params": _create_params(prompt="old prompt")}
    )
    job_id = create_resp.result["id"]

    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "cron.update",
            "params": {"id": job_id, "prompt": "new prompt"},
        }
    )
    assert resp.error is None
    assert resp.result["updated"] is True
    assert resp.result["job"]["prompt"] == "new prompt"

    # Verify persistence.
    updated_job = scheduler.get(job_id)
    assert updated_job is not None
    assert updated_job.prompt == "new prompt"


# ──────────────────────────────────────────────────────────
# 7. cron.update with no fields returns error
# ──────────────────────────────────────────────────────────

async def test_cron_update_no_fields(tmp_path) -> None:
    router, scheduler, run_store = _setup(tmp_path)
    create_resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "cron.create", "params": _create_params()}
    )
    job_id = create_resp.result["id"]

    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "cron.update", "params": {"id": job_id}}
    )
    assert resp.error is None
    assert resp.result["updated"] is False
    assert "field" in resp.result["error"].lower()


# ──────────────────────────────────────────────────────────
# 8. cron.update with invalid schedule returns error
# ──────────────────────────────────────────────────────────

async def test_cron_update_invalid_schedule(tmp_path) -> None:
    router, scheduler, run_store = _setup(tmp_path)
    create_resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "cron.create", "params": _create_params()}
    )
    job_id = create_resp.result["id"]

    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "cron.update",
            "params": {"id": job_id, "schedule": "not a cron"},
        }
    )
    assert resp.error is None
    assert resp.result["updated"] is False
    assert "invalid schedule" in resp.result["error"].lower()


# ──────────────────────────────────────────────────────────
# 9. cron.update valid schedule persists
# ──────────────────────────────────────────────────────────

async def test_cron_update_valid_schedule(tmp_path) -> None:
    router, scheduler, run_store = _setup(tmp_path)
    create_resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "cron.create", "params": _create_params()}
    )
    job_id = create_resp.result["id"]

    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "cron.update",
            "params": {"id": job_id, "schedule": "0 9 * * *"},
        }
    )
    assert resp.error is None
    assert resp.result["updated"] is True
    assert scheduler.get(job_id).schedule == "0 9 * * *"


# ──────────────────────────────────────────────────────────
# 10. cron.runs without run_store returns empty
# ──────────────────────────────────────────────────────────

async def test_cron_runs_no_run_store(tmp_path) -> None:
    job_store = CronJobStore(path=tmp_path / "cron.json")
    scheduler = CronScheduler(store=job_store, dispatcher=_dispatcher())
    router = Router()
    register_cron_methods(router, scheduler, run_store=None)

    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "cron.runs", "params": {}}
    )
    assert resp.error is None
    assert resp.result == {"runs": [], "total": 0, "has_more": False}
