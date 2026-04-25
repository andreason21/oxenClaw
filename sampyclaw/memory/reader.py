"""Range-read for files in the memory corpus.

Mirrors openclaw `host/read-file-shared.ts` `buildMemoryReadResultFromSlice`.
"""

from __future__ import annotations

from pathlib import Path

from sampyclaw.memory.models import MemoryReadResult


def read_file_range(
    corpus_dir: Path,
    rel_path: str,
    *,
    from_line: int = 1,
    lines: int = 120,
    max_chars: int = 12_000,
) -> MemoryReadResult:
    if from_line < 1:
        from_line = 1
    if lines < 1:
        lines = 1
    if max_chars < 1:
        max_chars = 1

    base = corpus_dir.resolve()
    target = (base / rel_path).resolve()
    if not target.is_relative_to(base):
        raise ValueError(f"path escapes corpus root: {rel_path!r}")
    if not target.exists() or not target.is_file():
        return MemoryReadResult(
            path=rel_path,
            text="",
            start_line=from_line,
            end_line=from_line - 1,
            truncated=False,
            next_from=None,
        )

    file_lines = target.read_text(encoding="utf-8", errors="replace").split("\n")
    selected = file_lines[from_line - 1 : from_line - 1 + lines]
    more_source = (from_line - 1 + len(selected)) < len(file_lines)
    fitted_text, included, hard_truncated = _fit_to_chars(selected, max_chars)
    char_truncated = hard_truncated or included < len(selected)
    next_from: int | None
    if not hard_truncated and (more_source or included < len(selected)):
        next_from = from_line + included
    else:
        next_from = None
    truncated = char_truncated or more_source
    end_line = from_line + included - 1 if included > 0 else from_line - 1
    return MemoryReadResult(
        path=rel_path,
        text=fitted_text,
        start_line=from_line,
        end_line=end_line,
        truncated=truncated,
        next_from=next_from,
    )


def _fit_to_chars(lines: list[str], max_chars: int) -> tuple[str, int, bool]:
    if not lines:
        return "", 0, False
    included = len(lines)
    text = "\n".join(lines)
    while included > 1 and len(text) > max_chars:
        included -= 1
        text = "\n".join(lines[:included])
    if len(text) <= max_chars:
        return text, included, False
    return text[:max_chars], 1, True
