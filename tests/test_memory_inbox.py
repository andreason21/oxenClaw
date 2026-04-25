"""inbox.append_to_inbox file format + idempotency."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sampyclaw.memory.inbox import append_to_inbox


def test_creates_file_when_missing(tmp_path: Path) -> None:
    path = tmp_path / "memory" / "inbox.md"
    when = append_to_inbox(path, "first note")
    assert path.exists()
    assert when in path.read_text()
    assert "first note" in path.read_text()


def test_appends_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "inbox.md"
    path.write_text("# preamble\n")
    append_to_inbox(path, "first")
    append_to_inbox(path, "second")
    body = path.read_text()
    assert body.startswith("# preamble")
    assert "first" in body and "second" in body


def test_tags_present_only_when_supplied(tmp_path: Path) -> None:
    path = tmp_path / "inbox.md"
    when_no = append_to_inbox(
        path, "no tags", now=datetime.fromisoformat("2026-04-25T01:00:00+00:00")
    )
    body_no = path.read_text()
    section_no = body_no.split(when_no, 1)[1]
    assert "**tags:**" not in section_no

    path2 = tmp_path / "inbox2.md"
    append_to_inbox(
        path2,
        "with tags",
        tags=["a", "b"],
        now=datetime.fromisoformat("2026-04-25T01:01:00+00:00"),
    )
    assert "**tags:** a, b" in path2.read_text()
