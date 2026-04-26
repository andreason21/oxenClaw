"""JSON-backed cron job store.

Atomic tmpfile+rename saves. Keyed by job id. Path defaults to
`~/.oxenclaw/cron/jobs.json`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from oxenclaw.config.paths import OxenclawPaths, default_paths
from oxenclaw.cron.models import CronJob


class CronJobStore:
    def __init__(self, path: Path | None = None, *, paths: OxenclawPaths | None = None) -> None:
        if path is None:
            resolved = paths or default_paths()
            path = resolved.home / "cron" / "jobs.json"
        self._path = path
        self._jobs: dict[str, CronJob] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        if not self._path.exists():
            return
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        for entry in raw.get("jobs", []):
            job = CronJob.model_validate(entry)
            self._jobs[job.id] = job

    def list(self) -> list[CronJob]:
        return sorted(self._jobs.values(), key=lambda j: j.id)

    def get(self, job_id: str) -> CronJob | None:
        return self._jobs.get(job_id)

    def add(self, job: CronJob) -> None:
        if job.id in self._jobs:
            raise ValueError(f"duplicate cron job id: {job.id}")
        self._jobs[job.id] = job

    def replace(self, job: CronJob) -> None:
        """Add or overwrite. Used by toggle/update paths."""
        self._jobs[job.id] = job

    def remove(self, job_id: str) -> bool:
        return self._jobs.pop(job_id, None) is not None

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {"jobs": [j.model_dump() for j in self.list()]}
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)

    def __len__(self) -> int:
        return len(self._jobs)
