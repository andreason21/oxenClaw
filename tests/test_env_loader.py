"""Tests for `~/.oxenclaw/env` read/write helpers."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from oxenclaw.config.env_loader import (
    env_file_path,
    load_oxenclaw_env_file,
    parse_env_file,
    persist_env_var,
)


def test_env_file_path_honours_oxenclaw_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    assert env_file_path() == tmp_path / "env"


def test_persist_env_var_writes_export_line_mode_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    path = persist_env_var("OPENAI_API_KEY", "sk-test")
    assert path == tmp_path / "env"
    assert 'export OPENAI_API_KEY="sk-test"' in path.read_text(encoding="utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_persist_env_var_upserts_existing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running replaces the prior value in place — no duplicate lines."""
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    persist_env_var("OPENAI_API_KEY", "old")
    persist_env_var("OPENAI_API_KEY", "new")
    text = (tmp_path / "env").read_text(encoding="utf-8")
    assert text.count("OPENAI_API_KEY") == 1
    assert 'export OPENAI_API_KEY="new"' in text
    assert "old" not in text


def test_persist_env_var_preserves_other_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    env = tmp_path / "env"
    env.write_text('export OXENCLAW_LLAMACPP_GGUF="/models/x.gguf"\n', encoding="utf-8")
    persist_env_var("GEMINI_API_KEY", "g-key")
    text = env.read_text(encoding="utf-8")
    assert "OXENCLAW_LLAMACPP_GGUF" in text
    assert 'export GEMINI_API_KEY="g-key"' in text
    # The merged file round-trips through the parser.
    parsed = parse_env_file(text)
    assert parsed["GEMINI_API_KEY"] == "g-key"
    assert parsed["OXENCLAW_LLAMACPP_GGUF"] == "/models/x.gguf"


def test_persisted_key_is_loaded_into_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    persist_env_var("OPENAI_API_KEY", "sk-loaded")
    applied = load_oxenclaw_env_file()
    assert applied >= 1
    import os

    assert os.environ.get("OPENAI_API_KEY") == "sk-loaded"
