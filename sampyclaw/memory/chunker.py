"""Markdown chunker — splits files by heading + size.

Returns `(start_line, end_line, text)` tuples with 1-indexed inclusive
line numbers. Strategy:

  1. Split by markdown headings (`^#+ `). Each chunk = heading line +
     body until the next same-or-higher-level heading. Heading-less
     preamble becomes its own chunk.
  2. If a chunk exceeds `max_chars`, subdivide by blank-line paragraphs.
  3. If a paragraph still exceeds `max_chars`, hard-split at boundaries.
  4. Merge adjacent chunks below `min_chars` until they reach the floor.
"""

from __future__ import annotations

import re

_HEADING_RE = re.compile(r"^(#+)\s")


def chunk_markdown(
    text: str,
    *,
    max_chars: int = 2000,
    min_chars: int = 200,
) -> list[tuple[int, int, str]]:
    if not text:
        return []
    lines = text.split("\n")
    sections = _split_by_headings(lines)
    sized: list[tuple[int, int, str]] = []
    for start, end, body in sections:
        if len(body) <= max_chars:
            sized.append((start, end, body))
            continue
        sized.extend(_split_oversize(body, start, max_chars))
    merged = _merge_small(sized, min_chars)
    return [(s, e, t.rstrip()) for s, e, t in merged if t.strip()]


def _split_by_headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """Walk lines, opening a new section at every heading.

    Heading level is the count of leading `#`. A new section opens when
    the level is at most the level of the active section's heading.
    """
    sections: list[tuple[int, int, str]] = []
    current_start = 1  # 1-indexed
    current_level = 0  # 0 = preamble (no heading yet)
    buf: list[str] = []

    def flush(end_line: int) -> None:
        if not buf:
            return
        body = "\n".join(buf)
        sections.append((current_start, end_line, body))

    for idx, line in enumerate(lines, start=1):
        m = _HEADING_RE.match(line)
        if m is not None:
            level = len(m.group(1))
            if buf and (current_level == 0 or level <= current_level):
                flush(idx - 1)
                buf = []
                current_start = idx
                current_level = level
            elif not buf:
                current_start = idx
                current_level = level
        buf.append(line)
    flush(len(lines))
    return sections


def _split_oversize(body: str, start_line: int, max_chars: int) -> list[tuple[int, int, str]]:
    """Subdivide an oversize section by blank-line paragraphs.

    Hard-splits a paragraph that is still over the limit at character
    boundaries. Line numbering is preserved end-to-end.
    """
    out: list[tuple[int, int, str]] = []
    lines = body.split("\n")
    para_start = start_line
    para_lines: list[str] = []

    def flush(end_line: int) -> None:
        nonlocal para_start
        if not para_lines:
            return
        joined = "\n".join(para_lines)
        if len(joined) <= max_chars:
            out.append((para_start, end_line, joined))
        else:
            out.extend(_hard_split(joined, para_start, max_chars))

    for idx, line in enumerate(lines):
        absolute_line = start_line + idx
        if line.strip() == "":
            if para_lines:
                flush(absolute_line - 1)
                para_lines = []
            para_start = absolute_line + 1
            continue
        para_lines.append(line)
    flush(start_line + len(lines) - 1)
    return out


def _hard_split(text: str, start_line: int, max_chars: int) -> list[tuple[int, int, str]]:
    """Split a single oversize blob at character boundaries.

    Lines are tracked through the slice so each output piece carries
    accurate `(start_line, end_line)` covering that text region.
    """
    out: list[tuple[int, int, str]] = []
    cursor = 0
    line_cursor = start_line
    while cursor < len(text):
        end = min(cursor + max_chars, len(text))
        piece = text[cursor:end]
        line_count = piece.count("\n")
        piece_end_line = line_cursor + line_count
        out.append((line_cursor, piece_end_line, piece))
        cursor = end
        # Next piece starts on the same line as the previous piece's last
        # line if the split fell mid-line, otherwise the line after.
        line_cursor = piece_end_line
    return out


def _merge_small(
    sections: list[tuple[int, int, str]], min_chars: int
) -> list[tuple[int, int, str]]:
    if not sections:
        return []
    merged: list[tuple[int, int, str]] = []
    for sec in sections:
        if merged and len(merged[-1][2]) < min_chars:
            prev_start, _, prev_text = merged[-1]
            _, new_end, new_text = sec
            merged[-1] = (prev_start, new_end, f"{prev_text}\n{new_text}")
        else:
            merged.append(sec)
    return merged
