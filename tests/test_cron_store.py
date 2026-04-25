"""Tests for CronJobStore: CRUD + atomic save/load."""

from __future__ import annotations

import pytest

from sampyclaw.cron.models import CronJob
from sampyclaw.cron.store import CronJobStore


def _job(job_id: str = "j1") -> CronJob:
    return CronJob(
        id=job_id,
        schedule="* * * * *",
        agent_id="assistant",
        channel="telegram",
        account_id="main",
        chat_id="42",
        prompt="ping",
    )


def test_empty_when_file_missing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = CronJobStore(path=tmp_path / "cron.json")
    assert store.list() == []


def test_add_persist_reload(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "cron" / "jobs.json"
    store = CronJobStore(path=path)
    store.add(_job())
    store.save()

    reloaded = CronJobStore(path=path)
    assert len(reloaded) == 1
    assert reloaded.get("j1").schedule == "* * * * *"


def test_add_rejects_duplicate(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = CronJobStore(path=tmp_path / "c.json")
    store.add(_job())
    with pytest.raises(ValueError):
        store.add(_job())


def test_replace_overwrites_existing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = CronJobStore(path=tmp_path / "c.json")
    store.add(_job())
    updated = _job().model_copy(update={"enabled": False})
    store.replace(updated)
    assert store.get("j1").enabled is False


def test_remove_returns_bool(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = CronJobStore(path=tmp_path / "c.json")
    store.add(_job())
    assert store.remove("j1") is True
    assert store.remove("j1") is False


def test_list_sorted_by_id(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = CronJobStore(path=tmp_path / "c.json")
    store.add(_job("b"))
    store.add(_job("a"))
    assert [j.id for j in store.list()] == ["a", "b"]


def test_save_atomic_no_tmp_left(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = CronJobStore(path=tmp_path / "c.json")
    store.add(_job())
    store.save()
    assert not any(p.suffix == ".tmp" for p in tmp_path.iterdir())
