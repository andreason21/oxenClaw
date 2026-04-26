"""Filesystem walker for the memory corpus.

Yields one tuple per markdown file: `(relpath, source, mtime, size, hash, text)`.
Hidden files/dirs (any path component starting with `.`) are skipped.

Guards:
- `max_depth` caps how far below `root` we descend (defends against deeply
  nested trees and accidental symlink loops).
- `max_file_size_bytes` skips files larger than the cap (defends against
  accidental log/dump files in the corpus that would OOM the indexer).
- Symlinks are not followed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from oxenclaw.memory.hashing import sha256_bytes
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("memory.walker")

DEFAULT_MAX_DEPTH = 16
DEFAULT_MAX_FILE_SIZE_BYTES = 4 * 1024 * 1024  # 4 MiB; markdown over this is a mistake


def scan_memory_dir(
    root: Path,
    source: str = "memory",
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
) -> Iterator[tuple[str, str, float, int, str, str]]:
    if not root.exists() or not root.is_dir():
        return
    root_resolved = root.resolve()
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if len(rel.parts) > max_depth:
            logger.debug("walker skipping (depth>%d): %s", max_depth, rel)
            continue
        # Don't follow symlinks out of the corpus root.
        if path.is_symlink():
            try:
                target = path.resolve(strict=False)
            except OSError:
                continue
            try:
                target.relative_to(root_resolved)
            except ValueError:
                logger.debug("walker skipping symlink outside root: %s", rel)
                continue
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size > max_file_size_bytes:
            logger.warning(
                "walker skipping oversized file %s (%d bytes > %d cap)",
                rel,
                stat.st_size,
                max_file_size_bytes,
            )
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        text = data.decode("utf-8", errors="replace")
        yield str(rel), source, stat.st_mtime, stat.st_size, sha256_bytes(data), text
