"""Three-layer tool-result persistence for context-window safety.

Defense against runaway context inflation operates at three levels:

1. **Per-tool output cap** (inside each tool): Tools self-truncate to a
   ceiling before returning. First line of defense, controlled by the
   tool author.

2. **Per-result persistence** (``maybe_persist_tool_result``): After a
   tool returns, if its output exceeds the resolved threshold for that
   tool, the full output is written atomically to
   ``storage_dir/{tool_use_id}.txt`` and the in-context content is
   replaced with a ``<persisted-output>`` block containing a 600-char
   preview plus the absolute path. The model can ``read_file`` against
   that path to inspect any windowed slice.

3. **Per-turn aggregate budget** (``enforce_turn_budget``): After all
   tool results in one assistant turn are collected, if the aggregate
   character count exceeds ``turn_budget`` (default 200K), the largest
   non-pinned, non-persisted entries are spilled to disk in priority
   order until the aggregate sits under budget.

Reads (``read_file``, ``memory_get``, ``memory_search``) are pinned by
default with effectively-unbounded thresholds because persisting a read
result and then asking the model to read THAT path is a useless round
trip — the model already had the content in front of it.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

PERSISTED_OUTPUT_TAG = "<persisted-output>"
PREVIEW_CHARS = 600
DEFAULT_TURN_BUDGET = 200_000

# Sentinel threshold used to mark a tool as effectively unbounded. We use a
# very large integer (10**12) rather than ``math.inf`` so callers can do
# ``len(s) > threshold`` integer comparisons without float coercion.
_UNBOUNDED = 10**12


@dataclass
class BudgetConfig:
    """Configuration for tool-result persistence and turn budget."""

    default_threshold: int = 8000
    pinned_thresholds: dict[str, int] = field(
        default_factory=lambda: {
            "read_file": _UNBOUNDED,
            "memory_get": _UNBOUNDED,
            "memory_search": _UNBOUNDED,
        }
    )


def resolve_threshold(tool_name: str, config: BudgetConfig) -> int:
    """Return the effective character threshold for a given tool."""
    if tool_name in config.pinned_thresholds:
        return config.pinned_thresholds[tool_name]
    return config.default_threshold


def _safe_join(storage_dir: Path, tool_use_id: str) -> Path:
    """Return ``storage_dir / {tool_use_id}.txt`` and refuse path escape.

    ``tool_use_id`` is normally an opaque token from the provider but we
    cannot trust it absolutely — a malicious or malformed value could
    contain ``..`` segments. Resolve both sides and assert containment.
    """
    storage_dir = storage_dir.resolve()
    candidate = (storage_dir / f"{tool_use_id}.txt").resolve()
    try:
        candidate.relative_to(storage_dir)
    except ValueError as exc:
        raise ValueError(
            f"unsafe tool_use_id {tool_use_id!r} escapes storage_dir {storage_dir}"
        ) from exc
    return candidate


def _atomic_write(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` atomically (tmp + os.replace)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, str(target))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _build_block(abs_path: Path, total_chars: int, preview: str) -> str:
    """Build the ``<persisted-output>`` replacement block."""
    return (
        f"\n[Output persisted to {abs_path} ({total_chars} chars). "
        "Use read_file with offset/limit to inspect.]\n"
        f"Preview (first {PREVIEW_CHARS} chars):\n"
        f"{preview}\n"
        "…[truncated, full file at path above]"
    )


def maybe_persist_tool_result(
    *,
    tool_use_id: str,
    tool_name: str,
    output: str,
    config: BudgetConfig,
    storage_dir: Path,
) -> str:
    """Layer 2: persist oversized output, return preview + path block.

    When ``len(output) <= resolve_threshold(tool_name, config)``, return
    the input unchanged. Otherwise write the full output to
    ``storage_dir/{tool_use_id}.txt`` (atomic) and return a
    ``<persisted-output>`` block.
    """
    threshold = resolve_threshold(tool_name, config)
    if len(output) <= threshold:
        return output

    target = _safe_join(storage_dir, tool_use_id)
    try:
        _atomic_write(target, output)
    except OSError as exc:
        logger.warning(
            "tool_result_storage: write failed for %s (%s) — returning original",
            tool_use_id,
            exc,
        )
        return output

    preview = output[:PREVIEW_CHARS]
    logger.info(
        "tool_result_storage: persisted %s (%s, %d chars -> %s)",
        tool_name,
        tool_use_id,
        len(output),
        target,
    )
    return _build_block(target, len(output), preview)


def _is_persisted(text: str) -> bool:
    """Heuristic: was this result already persisted in this turn?"""
    return "[Output persisted to" in text and "Use read_file with offset/limit" in text


def enforce_turn_budget(
    results: list[dict],
    config: BudgetConfig,
    storage_dir: Path,
    turn_budget: int = DEFAULT_TURN_BUDGET,
) -> int:
    """Layer 3: enforce aggregate budget across all tool results in one turn.

    Walks ``results`` (a list of ``{"id","name","output"}`` dicts), sums
    char counts; while the total exceeds ``turn_budget``, picks the
    largest non-pinned, non-persisted entry and persists it via
    ``maybe_persist_tool_result`` with threshold=0 forced. Returns the
    total number of characters that were spilled to disk.
    """
    total = sum(len(r.get("output", "") or "") for r in results)
    if total <= turn_budget:
        return 0

    pinned = set(config.pinned_thresholds.keys())
    persisted_chars = 0

    while total > turn_budget:
        # Pick largest non-pinned, non-persisted entry.
        candidate_idx = -1
        candidate_size = -1
        for i, r in enumerate(results):
            output = r.get("output", "") or ""
            if not output:
                continue
            if r.get("name") in pinned:
                continue
            if _is_persisted(output):
                continue
            if len(output) > candidate_size:
                candidate_idx = i
                candidate_size = len(output)

        if candidate_idx < 0:
            # No more candidates we can spill.
            break

        target = results[candidate_idx]
        original = target.get("output", "") or ""
        # Force-persist by using a config copy with threshold=0.
        forced_config = BudgetConfig(
            default_threshold=0,
            pinned_thresholds=config.pinned_thresholds,
        )
        replaced = maybe_persist_tool_result(
            tool_use_id=str(target.get("id") or f"budget_{candidate_idx}"),
            tool_name=str(target.get("name") or "__budget__"),
            output=original,
            config=forced_config,
            storage_dir=storage_dir,
        )
        if replaced == original:
            # Persistence failed (disk error, etc.) — bail out so we don't loop.
            break
        target["output"] = replaced
        delta = len(original) - len(replaced)
        total -= delta
        persisted_chars += len(original)
        logger.info(
            "turn_budget: persisted %s (%d chars; total now %d / budget %d)",
            target.get("id"),
            len(original),
            total,
            turn_budget,
        )

    return persisted_chars


__all__ = [
    "DEFAULT_TURN_BUDGET",
    "PERSISTED_OUTPUT_TAG",
    "PREVIEW_CHARS",
    "BudgetConfig",
    "enforce_turn_budget",
    "maybe_persist_tool_result",
    "resolve_threshold",
]
