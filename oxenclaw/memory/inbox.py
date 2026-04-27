"""Append-only inbox writer for the `memory_save` tool."""

from __future__ import annotations

import logging
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
