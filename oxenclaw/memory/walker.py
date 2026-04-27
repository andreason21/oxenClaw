"""Filesystem walker for the memory corpus.

Yields one tuple per markdown file: `(relpath, source, mtime, size, hash, text)`.
Hidden files/dirs (any path component starting with `.`) are skipped.

Guards:
- `max_depth` caps how far below `root` we descend (defends against deeply
  nested trees and accidental symlink loops).
- `max_file_size_bytes` skips files larger than the cap (defends against
  accidental log/dump files in the corpus that would OOM the indexer).
- Symlinks are not followed.
- `WalkerConfig` allow/deny glob lists filter relative paths; deny wins.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from oxenclaw.memory.hashing import sha256_bytes
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("memory.walker")

DEFAULT_MAX_DEPTH = 16
DEFAULT_MAX_FILE_SIZE_BYTES = 4 * 1024 * 1024  # 4 MiB; markdown over this is a mistake


@dataclass
class WalkerConfig:
    """Optional path-filter + size-cap config passed to ``scan_memory_dir``.

    ``allow_globs``  — if non-empty, only files whose relative path matches
                       at least one glob are included.
    ``deny_globs``   — files matching any deny glob are excluded, even if
                       they matched an allow glob (deny wins).
    ``min_size``     — skip files strictly smaller than this (bytes).
    ``max_size``     — skip files strictly larger than this (bytes);
                       0 means "use the caller's ``max_file_size_bytes``".
    """

    allow_globs: list[str] = field(default_factory=list)
    deny_globs: list[str] = field(default_factory=list)
    min_size: int = 0
    max_size: int = 0  # 0 = defer to max_file_size_bytes argument


def _glob_match(rel: str, globs: list[str]) -> bool:
    """Return True if *rel* matches any pattern in *globs*."""
    return any(fnmatch.fnmatch(rel, g) for g in globs)


def scan_memory_dir(
    root: Path,
    source: str = "memory",
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
    walker_config: WalkerConfig | None = None,
) -> Iterator[tuple[str, str, float, int, str, str]]:
    if not root.exists() or not root.is_dir():
        return
    root_resolved = root.resolve()
    cfg = walker_config  # shorthand

    # Effective max size: WalkerConfig.max_size overrides the argument when set.
    effective_max_size = (
        cfg.max_size if (cfg is not None and cfg.max_size > 0) else max_file_size_bytes
    )
    effective_min_size = cfg.min_size if cfg is not None else 0

    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root)
        rel_str = str(rel)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if len(rel.parts) > max_depth:
            logger.debug("walker skipping (depth>%d): %s", max_depth, rel)
            continue

        # Allow/deny glob filtering (deny wins over allow).
        if cfg is not None:
            if cfg.deny_globs and _glob_match(rel_str, cfg.deny_globs):
                logger.debug("walker skipping (deny glob): %s", rel_str)
                continue
            if cfg.allow_globs and not _glob_match(rel_str, cfg.allow_globs):
                logger.debug("walker skipping (not in allow globs): %s", rel_str)
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

        # min_size check
        if stat.st_size < effective_min_size:
            logger.debug(
                "walker skipping undersized file %s (%d bytes < %d min)",
                rel,
                stat.st_size,
                effective_min_size,
            )
            continue

        if stat.st_size > effective_max_size:
            logger.warning(
                "walker skipping oversized file %s (%d bytes > %d cap)",
                rel,
                stat.st_size,
                effective_max_size,
            )
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        text = data.decode("utf-8", errors="replace")
        yield str(rel), source, stat.st_mtime, stat.st_size, sha256_bytes(data), text
