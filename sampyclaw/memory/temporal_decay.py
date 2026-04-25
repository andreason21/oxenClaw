"""Half-life-based temporal decay for memory search scores.

Mirrors openclaw `extensions/memory-core/src/memory/temporal-decay.ts`.
Older chunks are penalised by ``exp(-ln(2)/halflife * age_days)``. Memory
files whose path encodes a date (``memory/YYYY-MM-DD.md``) use the embedded
date; other files fall back to file mtime. Evergreen files (``MEMORY.md``
or any non-dated path under ``memory/``) opt out of decay.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sampyclaw.memory.models import MemorySearchResult

DAY_SECONDS = 86_400

_DATED_PATH_RE = re.compile(r"(?:^|/)memory/(\d{4})-(\d{2})-(\d{2})\.md$")
_MEMORY_ROOT_RE = re.compile(r"^memory/")


@dataclass(frozen=True)
class TemporalDecayConfig:
    """Half-life decay knobs. ``half_life_days <= 0`` disables decay."""

    enabled: bool = False
    half_life_days: float = 30.0


DEFAULT_TEMPORAL_DECAY_CONFIG = TemporalDecayConfig()


def _normalise(relpath: str) -> str:
    return relpath.replace("\\", "/").removeprefix("./")


def parse_memory_date_from_path(relpath: str) -> datetime | None:
    """Parse a ``memory/YYYY-MM-DD.md`` path into a UTC midnight datetime."""
    normalised = _normalise(relpath)
    match = _DATED_PATH_RE.search(normalised)
    if match is None:
        return None
    try:
        year = int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3))
    except ValueError:
        return None
    try:
        return datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None


def is_evergreen_memory_path(relpath: str) -> bool:
    """``MEMORY.md`` or any non-dated file under ``memory/`` is evergreen."""
    normalised = _normalise(relpath)
    if normalised == "MEMORY.md":
        return True
    if not _MEMORY_ROOT_RE.match(normalised):
        return False
    return _DATED_PATH_RE.search(normalised) is None


def to_decay_lambda(half_life_days: float) -> float:
    """``ln(2) / halflife``. Returns 0 for non-positive or non-finite input."""
    if not math.isfinite(half_life_days) or half_life_days <= 0:
        return 0.0
    return math.log(2) / half_life_days


def decay_multiplier(*, age_in_days: float, half_life_days: float) -> float:
    """``exp(-lambda * age)``. Returns 1.0 if decay is effectively disabled."""
    lam = to_decay_lambda(half_life_days)
    if not math.isfinite(age_in_days):
        return 1.0
    clamped_age = max(0.0, age_in_days)
    if lam <= 0:
        return 1.0
    return math.exp(-lam * clamped_age)


def apply_decay(*, score: float, age_in_days: float, half_life_days: float) -> float:
    """Multiply ``score`` by the decay multiplier."""
    return score * decay_multiplier(age_in_days=age_in_days, half_life_days=half_life_days)


def chunk_age_days(
    *,
    chunk_path: str,
    file_mtime_seconds: float,
    now_seconds: float,
) -> float | None:
    """Return age in days, preferring path-embedded date, else mtime.

    Returns ``None`` for evergreen paths (no decay should be applied).
    """
    dated = parse_memory_date_from_path(chunk_path)
    if dated is not None:
        age_seconds = max(0.0, now_seconds - dated.timestamp())
        return age_seconds / DAY_SECONDS

    if is_evergreen_memory_path(chunk_path):
        return None

    if not math.isfinite(file_mtime_seconds):
        return None

    age_seconds = max(0.0, now_seconds - file_mtime_seconds)
    return age_seconds / DAY_SECONDS


def apply_temporal_decay_to_results(
    results: list[MemorySearchResult],
    *,
    file_mtimes: dict[str, float],
    config: TemporalDecayConfig = DEFAULT_TEMPORAL_DECAY_CONFIG,
    now_seconds: float | None = None,
) -> list[MemorySearchResult]:
    """Return a new list with score multiplied by decay; re-sorted desc."""
    if not config.enabled:
        return list(results)

    now = now_seconds if now_seconds is not None else datetime.now(UTC).timestamp()
    decayed: list[MemorySearchResult] = []
    for r in results:
        mtime = file_mtimes.get(r.chunk.path, float("nan"))
        age = chunk_age_days(
            chunk_path=r.chunk.path,
            file_mtime_seconds=mtime,
            now_seconds=now,
        )
        if age is None:
            decayed.append(r)
            continue
        new_score = apply_decay(
            score=r.score,
            age_in_days=age,
            half_life_days=config.half_life_days,
        )
        decayed.append(
            MemorySearchResult(
                chunk=r.chunk,
                score=new_score,
                distance=r.distance,
            )
        )
    decayed.sort(key=lambda r: r.score, reverse=True)
    return decayed
