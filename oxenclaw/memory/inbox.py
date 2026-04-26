"""Append-only inbox writer for the `memory_save` tool."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


def append_to_inbox(
    inbox_path: Path,
    text: str,
    tags: list[str] | None = None,
    now: datetime | None = None,
) -> str:
    """Append a timestamped section to `inbox_path`. Returns the heading text."""
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
