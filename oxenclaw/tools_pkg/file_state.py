"""Cross-agent file state registry.

When multiple agents (subagents, parallel tools) share the same
filesystem, they can clobber each other's edits if A reads a file,
spends a few seconds doing other work, then writes back stale content
that misses B's intervening edit.

This module ships a process-wide registry that tracks, per
``(task_id, abs_path)``:

  * ``last_read_at``     — wall-clock when the agent last saw the file
  * ``last_read_mtime``  — disk mtime captured at the time of read
  * ``partial_read``     — True when only a slice was read

Plus a global ``last_writer`` map (``abs_path -> task_id``) and a
lazy per-path ``threading.Lock``.

The fs_tools edit/write paths consult ``check_stale`` BEFORE writing.
The result, if any, is a discriminated record with ``kind`` ∈
{``"sibling_wrote"``, ``"mtime_drift"``, ``"write_without_read"``}.
The hook is informative — it warns rather than blocks; the agent can
still re-read and try again.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal


StaleKind = Literal["sibling_wrote", "mtime_drift", "write_without_read"]


@dataclass(frozen=True)
class StaleWarning:
    kind: StaleKind
    message: str
    last_writer: str | None = None
    last_writer_at: float | None = None


@dataclass
class _ReadStamp:
    last_read_at: float
    last_read_mtime: float
    partial_read: bool


class FileStateRegistry:
    """Process-wide coordinator. One singleton per Python process."""

    def __init__(self) -> None:
        # (task_id, abs_path) -> _ReadStamp
        self._reads: dict[tuple[str, str], _ReadStamp] = {}
        # abs_path -> (task_id, write_ts)
        self._last_writer: dict[str, tuple[str, float]] = {}
        # abs_path -> Lock (lazy)
        self._path_locks: dict[str, threading.Lock] = {}
        self._meta_lock = threading.Lock()
        self._state_lock = threading.Lock()

    @staticmethod
    def _resolve(path: str | Path) -> str:
        return str(Path(path).resolve())

    def lock_for(self, path: str | Path) -> threading.Lock:
        """Return a per-path lock (created lazily)."""
        resolved = self._resolve(path)
        with self._meta_lock:
            lock = self._path_locks.get(resolved)
            if lock is None:
                lock = threading.Lock()
                self._path_locks[resolved] = lock
            return lock

    def register_read(
        self,
        task_id: str,
        path: str | Path,
        mtime: float | None = None,
        partial: bool = False,
    ) -> None:
        resolved = self._resolve(path)
        if mtime is None:
            try:
                mtime = os.path.getmtime(resolved)
            except OSError:
                return
        with self._state_lock:
            self._reads[(task_id, resolved)] = _ReadStamp(
                last_read_at=time.time(),
                last_read_mtime=float(mtime),
                partial_read=bool(partial),
            )

    def register_write(
        self,
        task_id: str,
        path: str | Path,
        mtime: float | None = None,
    ) -> None:
        resolved = self._resolve(path)
        now = time.time()
        if mtime is None:
            try:
                mtime = os.path.getmtime(resolved)
            except OSError:
                mtime = now
        with self._state_lock:
            self._last_writer[resolved] = (task_id, now)
            # A write counts as an implicit fresh read for THIS agent.
            self._reads[(task_id, resolved)] = _ReadStamp(
                last_read_at=now,
                last_read_mtime=float(mtime),
                partial_read=False,
            )

    def check_stale(
        self,
        task_id: str,
        path: str | Path,
    ) -> StaleWarning | None:
        """Return a warning if ``task_id`` is about to write stale content."""
        resolved = self._resolve(path)
        with self._state_lock:
            stamp = self._reads.get((task_id, resolved))
            last_writer = self._last_writer.get(resolved)

        # Sibling wrote after our last read — most severe class.
        if last_writer is not None:
            writer_tid, writer_ts = last_writer
            if writer_tid != task_id:
                if stamp is None:
                    return StaleWarning(
                        kind="sibling_wrote",
                        message=(
                            f"another agent ({writer_tid!r}) wrote to "
                            f"{resolved} but this agent never read it"
                        ),
                        last_writer=writer_tid,
                        last_writer_at=writer_ts,
                    )
                if writer_ts > stamp.last_read_at:
                    return StaleWarning(
                        kind="sibling_wrote",
                        message=(
                            f"another agent ({writer_tid!r}) wrote to "
                            f"{resolved} after this agent's last read"
                        ),
                        last_writer=writer_tid,
                        last_writer_at=writer_ts,
                    )

        # Disk mtime drifted from our recorded value (external editor / cron).
        if stamp is not None:
            try:
                disk_mtime = os.path.getmtime(resolved)
            except OSError:
                disk_mtime = None
            if disk_mtime is not None and disk_mtime != stamp.last_read_mtime:
                return StaleWarning(
                    kind="mtime_drift",
                    message=(
                        f"{resolved} was modified on disk after this "
                        "agent's last read"
                    ),
                )
            if stamp.partial_read:
                return StaleWarning(
                    kind="mtime_drift",
                    message=(
                        f"{resolved} was last read with offset/limit pagination"
                    ),
                )

        # Never read — net-new file is fine, but if it exists and we have
        # no stamp, warn so the agent re-reads first.
        if stamp is None:
            try:
                exists = os.path.exists(resolved)
            except OSError:
                exists = False
            if exists:
                return StaleWarning(
                    kind="write_without_read",
                    message=(
                        f"{resolved} exists but was not read by this agent"
                    ),
                )
        return None

    def writes_since(
        self,
        task_id: str,
        paths_read: Iterable[str | Path],
    ) -> list[str]:
        """Return paths from ``paths_read`` that were written by other
        agents AFTER this agent's last read."""
        resolved_paths = [self._resolve(p) for p in paths_read]
        out: list[str] = []
        with self._state_lock:
            for p in resolved_paths:
                last_writer = self._last_writer.get(p)
                if last_writer is None:
                    continue
                writer_tid, writer_ts = last_writer
                if writer_tid == task_id:
                    continue
                stamp = self._reads.get((task_id, p))
                if stamp is None or writer_ts > stamp.last_read_at:
                    out.append(p)
        return out

    # Test hooks
    def clear(self) -> None:
        with self._state_lock:
            self._reads.clear()
            self._last_writer.clear()
        with self._meta_lock:
            self._path_locks.clear()


_REGISTRY = FileStateRegistry()


def get_registry() -> FileStateRegistry:
    return _REGISTRY


__all__ = [
    "FileStateRegistry",
    "StaleKind",
    "StaleWarning",
    "get_registry",
]
