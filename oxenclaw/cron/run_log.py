"""Cron run-history store — JSON-backed, atomic writes.

Each scheduled job fire gets a ``CronRunEntry``. Entries start with
``status="running"`` then transition to ``"ok"``/``"error"``/``"skipped"``
via ``update()``.

File lives at ``<paths.home>/cron/runs.json`` by default. Writes are
atomic (tmp + os.replace). Pruning caps per-job history at
``max_per_job`` entries (default 100), dropping oldest first.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def _new_run_id() -> str:
    return uuid4().hex


class CronRunEntry(BaseModel):
    """Single run record for a cron job fire."""

    run_id: str = Field(default_factory=_new_run_id)
    job_id: str
    started_at: float
    ended_at: float | None = None
    status: Literal["ok", "error", "skipped", "running"] = "running"
    summary: str = ""
    output_preview: str = ""
    error: str | None = None
    model: str | None = None
    provider: str | None = None
    token_usage: dict[str, int] | None = None
    delivery_status: Literal["delivered", "failed", "skipped"] | None = None


class CronRunStore:
    """JSON-backed store for cron run history.

    All mutation methods update the in-memory list and immediately persist
    via an atomic tmp+rename write. ``prune()`` removes oldest entries
    when a job exceeds *max_per_job* runs.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: list[CronRunEntry] = []
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for item in raw.get("runs", []):
            try:
                self._entries.append(CronRunEntry.model_validate(item))
            except Exception:
                pass  # skip corrupt entries

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {"runs": [e.model_dump() for e in self._entries]}
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def append(self, entry: CronRunEntry) -> None:
        """Add a new run entry and persist."""
        self._entries.append(entry)
        self._save()

    def update(self, run_id: str, **fields: object) -> CronRunEntry | None:
        """Update fields on an existing entry. Returns the updated entry or None."""
        for i, entry in enumerate(self._entries):
            if entry.run_id == run_id:
                updated = entry.model_copy(update=fields)
                self._entries[i] = updated
                self._save()
                return updated
        return None

    def list(
        self,
        *,
        job_id: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
        query: str | None = None,
        sort_dir: str = "desc",
        delivery: list[str] | None = None,
    ) -> list[CronRunEntry]:
        """Return a paginated, optionally filtered slice of run entries."""
        entries = self._filtered(
            job_id=job_id, statuses=statuses, query=query, delivery=delivery
        )
        if sort_dir == "asc":
            entries = sorted(entries, key=lambda e: e.started_at)
        else:
            entries = sorted(entries, key=lambda e: e.started_at, reverse=True)
        return entries[offset : offset + limit]

    def total(
        self,
        *,
        job_id: str | None = None,
        statuses: list[str] | None = None,
        query: str | None = None,
        delivery: list[str] | None = None,
    ) -> int:
        """Count entries matching the given filters (no pagination)."""
        return len(
            self._filtered(job_id=job_id, statuses=statuses, query=query, delivery=delivery)
        )

    def prune(self, max_per_job: int = 100) -> int:
        """Drop oldest entries so each job keeps at most *max_per_job* runs.

        Returns the number of entries removed.
        """
        by_job: dict[str, list[CronRunEntry]] = {}
        for entry in self._entries:
            by_job.setdefault(entry.job_id, []).append(entry)

        removed = 0
        new_entries: list[CronRunEntry] = []
        for job_entries in by_job.values():
            # Sort oldest-first; keep only the last *max_per_job*.
            sorted_entries = sorted(job_entries, key=lambda e: e.started_at)
            if len(sorted_entries) > max_per_job:
                removed += len(sorted_entries) - max_per_job
                sorted_entries = sorted_entries[-max_per_job:]
            new_entries.extend(sorted_entries)

        if removed:
            self._entries = new_entries
            self._save()
        return removed

    # ------------------------------------------------------------------
    # Private filtering
    # ------------------------------------------------------------------

    def _filtered(
        self,
        *,
        job_id: str | None,
        statuses: list[str] | None,
        query: str | None,
        delivery: list[str] | None,
    ) -> list[CronRunEntry]:
        q = query.lower() if query else None
        result: list[CronRunEntry] = []
        for entry in self._entries:
            if job_id is not None and entry.job_id != job_id:
                continue
            if statuses is not None and entry.status not in statuses:
                continue
            if delivery is not None and entry.delivery_status not in delivery:
                continue
            if q is not None:
                haystack = " ".join(
                    filter(
                        None,
                        [entry.summary, entry.output_preview, entry.error or ""],
                    )
                ).lower()
                if q not in haystack:
                    continue
            result.append(entry)
        return result
