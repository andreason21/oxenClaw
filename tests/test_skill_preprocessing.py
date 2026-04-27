"""Tests for skill body preprocessing (template substitution)."""

from __future__ import annotations

from pathlib import Path

from oxenclaw.clawhub.preprocessing import preprocess_skill_body


def test_substitute_skill_dir(tmp_path: Path) -> None:
    body = "Run scripts in ${OXENCLAW_SKILL_DIR}/scripts."
    out = preprocess_skill_body(body, skill_dir=tmp_path / "weather", session_id="abc")
    assert "${OXENCLAW_SKILL_DIR}" not in out
    assert str(tmp_path / "weather") in out


def test_substitute_session_id(tmp_path: Path) -> None:
    body = "Session is ${OXENCLAW_SESSION_ID}"
    out = preprocess_skill_body(body, skill_dir=tmp_path, session_id="sess-42")
    assert out == "Session is sess-42"


def test_inline_shell_off_by_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OXENCLAW_ALLOW_SKILL_INLINE_SHELL", raising=False)
    body = "today: !`echo hi`"
    out = preprocess_skill_body(body, skill_dir=tmp_path)
    # Default-OFF: leaves the literal in place.
    assert "!`echo hi`" in out


def test_inline_shell_runs_when_opted_in(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OXENCLAW_ALLOW_SKILL_INLINE_SHELL", "1")
    body = "today: !`echo hi`"
    out = preprocess_skill_body(body, skill_dir=tmp_path)
    assert "hi" in out
    assert "!`echo hi`" not in out


def test_session_id_empty_when_unknown(tmp_path: Path) -> None:
    body = "session=${OXENCLAW_SESSION_ID}!"
    out = preprocess_skill_body(body, skill_dir=tmp_path)
    assert out == "session=!"
