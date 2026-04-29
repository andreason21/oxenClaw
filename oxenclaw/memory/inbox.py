"""Append-only inbox writer for the `memory_save` tool.

Dedup utilities live here too: `parse_inbox()` reads the existing
sections back out, and `remove_inbox_entry()` rewrites the file with
one section dropped. The retriever uses these to implement
"if same content, update" — remove the old section, append a new
one with the current timestamp + merged tags, so the inbox never
accumulates 11 near-identical "user lives in Suwon" rows.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

_logger = logging.getLogger("memory.inbox")


def append_to_inbox(
    inbox_path: Path,
    text: str,
    tags: list[str] | None = None,
    now: datetime | None = None,
    redact_level: str | None = None,
) -> str:
    """Append a timestamped section to ``inbox_path``. Returns the heading text.

    Parameters
    ----------
    redact_level:
        When set to ``"light"`` or ``"strict"``, the text is run through
        :func:`oxenclaw.memory.privacy.redact` before writing. A ``WARNING``
        log line summarises the number of redacted items without echoing the
        raw spans.
    """
    if redact_level is not None and redact_level != "off":
        from oxenclaw.memory.privacy import redact as _redact

        text, hits = _redact(text, level=redact_level)  # type: ignore[arg-type]
        if hits:
            _logger.warning(
                "redacted %d secret(s) from inbox entry",
                len(hits),
            )

    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    when = (now or datetime.now().astimezone()).isoformat()
    parts: list[str] = ["", f"## {when}", ""]
    if tags:
        parts.extend([f"**tags:** {', '.join(tags)}", ""])
    parts.append(text.rstrip())
    parts.append("")
    block = "\n".join(parts)
    existing = ""
    if inbox_path.exists():
        existing = inbox_path.read_text(encoding="utf-8")
    inbox_path.write_text(existing + block, encoding="utf-8")
    return when


# ─── dedup helpers ────────────────────────────────────────────────────


_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_TAG_LINE_RE = re.compile(r"^\*\*tags:\*\*\s*(.+?)\s*$")
_NORMALIZE_PUNCT_RE = re.compile(r"[\s　]+")
# Trailing punctuation we strip before comparing — full-stop variants in
# both ASCII and Korean. Apostrophe stays so "user's" doesn't collide
# with "users".
_TRIM_TRAILING = ".,!?。、！？ \t\n"


@dataclass
class InboxEntry:
    """One `## <timestamp>` section parsed back out of the inbox file."""

    when: str
    body: str
    tags: list[str] = field(default_factory=list)
    # 1-indexed inclusive line numbers of the entire section (heading
    # through last body line). `remove_inbox_entry` uses these to slice
    # the file.
    start_line: int = 0
    end_line: int = 0


def parse_inbox(inbox_path: Path) -> list[InboxEntry]:
    """Return the inbox sections in file order. Empty list when the
    file is missing / empty."""
    if not inbox_path.exists():
        return []
    text = inbox_path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    lines = text.split("\n")

    entries: list[InboxEntry] = []
    cur_start = -1  # 0-indexed line of current `##` header
    cur_when = ""
    cur_tags: list[str] = []
    body_lines: list[str] = []
    last_nonblank = -1  # 0-indexed of last non-blank body line

    def flush() -> None:
        nonlocal cur_start, cur_when, cur_tags, body_lines, last_nonblank
        if cur_start < 0:
            return
        body = "\n".join(body_lines).strip("\n")
        body = body.strip()
        if body or cur_tags:
            end = (last_nonblank if last_nonblank >= 0 else cur_start) + 1
            entries.append(
                InboxEntry(
                    when=cur_when,
                    body=body,
                    tags=list(cur_tags),
                    start_line=cur_start + 1,
                    end_line=end,
                )
            )
        cur_start = -1
        cur_when = ""
        cur_tags = []
        body_lines = []
        last_nonblank = -1

    for i, line in enumerate(lines):
        h = _HEADING_RE.match(line)
        if h is not None:
            flush()
            cur_start = i
            cur_when = h.group(1).strip()
            cur_tags = []
            body_lines = []
            last_nonblank = i  # heading itself counts as a non-blank line
            continue
        if cur_start < 0:
            continue  # preamble / no current section
        t = _TAG_LINE_RE.match(line)
        # A tag line is only the section's tag header when no body
        # text has been collected yet — leading blank lines between
        # heading and tag-line are tolerated. A "**tags:**" string
        # mid-body stays in the body verbatim.
        no_body_yet = all(not bl.strip() for bl in body_lines)
        if t is not None and no_body_yet:
            cur_tags = [s.strip() for s in t.group(1).split(",") if s.strip()]
            body_lines = []  # discard the leading blanks above the tag line
            last_nonblank = i
            continue
        body_lines.append(line)
        if line.strip():
            last_nonblank = i
    flush()
    return entries


def remove_inbox_entry(inbox_path: Path, entry: InboxEntry) -> bool:
    """Rewrite ``inbox_path`` without ``entry`` (removes the heading,
    its tag line if present, body, and any trailing blank lines that
    belonged to the section). Atomic via tempfile + rename.

    Returns True when the entry was found and removed; False when the
    file no longer contains it (rare race when two concurrent saves
    target the same section)."""
    if not inbox_path.exists():
        return False
    text = inbox_path.read_text(encoding="utf-8")
    lines = text.split("\n")
    s, e = entry.start_line, entry.end_line
    if s < 1 or e < s or s > len(lines):
        return False
    # Convert to 0-indexed half-open slice.
    drop_start = s - 1
    drop_end = min(e, len(lines))
    # Sweep blank lines that immediately follow the section so the file
    # doesn't accumulate a growing run of empty lines after each
    # dedup-replace cycle.
    while drop_end < len(lines) and lines[drop_end].strip() == "":
        drop_end += 1
    new_lines = lines[:drop_start] + lines[drop_end:]
    new_text = "\n".join(new_lines)
    tmp = inbox_path.with_suffix(inbox_path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, inbox_path)
    return True


def normalize_for_dedup(text: str) -> str:
    """Cheap text-equality key used by the dedup layer-1 fast path.

    Folds case, collapses whitespace runs (incl. CJK ideographic
    space), and strips trailing punctuation/whitespace so

      "user lives in Suwon" / "User lives in Suwon." / "  user lives in
      suwon!  " / "User Lives In Suwon\n"

    all hash to the same key. Does NOT do semantic normalization
    (translation, paraphrase) — that's the embedding layer's job.
    """
    s = text.strip().lower()
    s = _NORMALIZE_PUNCT_RE.sub(" ", s)
    return s.strip(_TRIM_TRAILING).strip()
