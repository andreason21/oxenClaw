"""Tests for CronRunStore (≥8 tests).

Covers: append → list; running → ok update with output_preview; prune caps
per-job; query substring filter; status multi-filter; offset/limit pagination;
total counts; delivery filter.
"""

from __future__ import annotations

import time

import pytest

from oxenclaw.cron.run_log import CronRunEntry, CronRunStore


def _entry(job_id: str = "job1", status: str = "ok", summary: str = "", error: str | None = None, output_preview: str = "") -> CronRunEntry:
    return CronRunEntry(
        job_id=job_id,
        started_at=time.time(),
        ended_at=time.time() + 1.0,
        status=status,  # type: ignore[arg-type]
        summary=summary,
        output_preview=output_preview,
        error=error,
    )


# ──────────────────────────────────────────────────────────
# 1. append → list returns it
# ──────────────────────────────────────────────────────────

def test_append_list_round_trip(tmp_path) -> None:
    store = CronRunStore(tmp_path / "runs.json")
    entry = _entry(summary="hello")
    store.append(entry)
    found = store.list()
    assert len(found) == 1
    assert found[0].run_id == entry.run_id
    assert found[0].summary == "hello"


# ──────────────────────────────────────────────────────────
# 2. running → update to ok with output_preview
# ──────────────────────────────────────────────────────────

def test_update_running_to_ok(tmp_path) -> None:
    store = CronRunStore(tmp_path / "runs.json")
    entry = CronRunEntry(job_id="j1", started_at=time.time(), status="running")
    store.append(entry)

    updated = store.update(entry.run_id, status="ok", output_preview="first 200 chars")
    assert updated is not None
    assert updated.status == "ok"
    assert updated.output_preview == "first 200 chars"

    # Persisted correctly.
    reloaded = CronRunStore(tmp_path / "runs.json")
    result = reloaded.list()
    assert len(result) == 1
    assert result[0].status == "ok"
    assert result[0].output_preview == "first 200 chars"


# ──────────────────────────────────────────────────────────
# 3. prune caps per-job at max_per_job
# ──────────────────────────────────────────────────────────

def test_prune_caps_per_job(tmp_path) -> None:
    store = CronRunStore(tmp_path / "runs.json")
    for i in range(15):
        e = CronRunEntry(job_id="j1", started_at=float(i))
        store.append(e)
    assert len(store.list(limit=100)) == 15

    removed = store.prune(max_per_job=10)
    assert removed == 5
    assert len(store.list(limit=100)) == 10

    # Oldest 5 removed — verify the latest are kept.
    remaining_starts = sorted(e.started_at for e in store.list(limit=100))
    assert remaining_starts[0] == 5.0
    assert remaining_starts[-1] == 14.0


def test_prune_multiple_jobs_independent(tmp_path) -> None:
    store = CronRunStore(tmp_path / "runs.json")
    for i in range(12):
        store.append(CronRunEntry(job_id="ja", started_at=float(i)))
    for i in range(5):
        store.append(CronRunEntry(job_id="jb", started_at=float(i)))

    removed = store.prune(max_per_job=10)
    assert removed == 2
    assert store.total(job_id="ja") == 10
    assert store.total(job_id="jb") == 5


# ──────────────────────────────────────────────────────────
# 4. query substring filter against summary / output_preview / error
# ──────────────────────────────────────────────────────────

def test_query_filter_summary(tmp_path) -> None:
    store = CronRunStore(tmp_path / "runs.json")
    store.append(_entry(summary="morning report generated"))
    store.append(_entry(summary="irrelevant"))
    store.append(_entry(error="connection timeout", status="error"))

    results = store.list(query="morning")
    assert len(results) == 1
    assert results[0].summary == "morning report generated"

    results_error = store.list(query="timeout")
    assert len(results_error) == 1
    assert results_error[0].error == "connection timeout"


# ──────────────────────────────────────────────────────────
# 5. status multi-filter
# ──────────────────────────────────────────────────────────

def test_status_multi_filter(tmp_path) -> None:
    store = CronRunStore(tmp_path / "runs.json")
    store.append(_entry(status="ok"))
    store.append(_entry(status="error"))
    store.append(_entry(status="skipped"))
    store.append(_entry(status="running"))

    result = store.list(statuses=["ok", "error"])
    statuses = {e.status for e in result}
    assert statuses == {"ok", "error"}
    assert len(result) == 2


# ──────────────────────────────────────────────────────────
# 6. offset/limit pagination
# ──────────────────────────────────────────────────────────

def test_offset_limit(tmp_path) -> None:
    store = CronRunStore(tmp_path / "runs.json")
    # Insert 10 entries with known started_at so sort order is deterministic.
    for i in range(10):
        store.append(CronRunEntry(job_id="j1", started_at=float(i)))

    # Default sort is desc (newest first).
    page1 = store.list(limit=3, offset=0)
    page2 = store.list(limit=3, offset=3)
    assert len(page1) == 3
    assert len(page2) == 3
    # No overlap.
    ids1 = {e.run_id for e in page1}
    ids2 = {e.run_id for e in page2}
    assert ids1.isdisjoint(ids2)


# ──────────────────────────────────────────────────────────
# 7. total() counts correctly
# ──────────────────────────────────────────────────────────

def test_total_counts(tmp_path) -> None:
    store = CronRunStore(tmp_path / "runs.json")
    store.append(_entry(job_id="j1", status="ok"))
    store.append(_entry(job_id="j1", status="error"))
    store.append(_entry(job_id="j2", status="ok"))

    assert store.total() == 3
    assert store.total(job_id="j1") == 2
    assert store.total(job_id="j2") == 1
    assert store.total(statuses=["ok"]) == 2
    assert store.total(statuses=["error"]) == 1


# ──────────────────────────────────────────────────────────
# 8. delivery_status filter
# ──────────────────────────────────────────────────────────

def test_delivery_status_filter(tmp_path) -> None:
    store = CronRunStore(tmp_path / "runs.json")
    e1 = CronRunEntry(job_id="j1", started_at=1.0, delivery_status="delivered")
    e2 = CronRunEntry(job_id="j1", started_at=2.0, delivery_status="failed")
    e3 = CronRunEntry(job_id="j1", started_at=3.0, delivery_status="skipped")
    for e in (e1, e2, e3):
        store.append(e)

    delivered = store.list(delivery=["delivered"])
    assert len(delivered) == 1
    assert delivered[0].delivery_status == "delivered"

    failed_skipped = store.list(delivery=["failed", "skipped"])
    assert len(failed_skipped) == 2


# ──────────────────────────────────────────────────────────
# 9. atomic write leaves no tmp file behind
# ──────────────────────────────────────────────────────────

def test_atomic_write_no_tmp(tmp_path) -> None:
    store = CronRunStore(tmp_path / "runs.json")
    store.append(_entry())
    tmps = list((tmp_path).glob("*.tmp"))
    assert tmps == []


# ──────────────────────────────────────────────────────────
# 10. update unknown run_id returns None
# ──────────────────────────────────────────────────────────

def test_update_unknown_run_id_returns_none(tmp_path) -> None:
    store = CronRunStore(tmp_path / "runs.json")
    result = store.update("does-not-exist", status="ok")
    assert result is None
