"""Typer CLI smoke tests: subcommand registration, config show/validate, paths."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from oxenclaw.cli.__main__ import app

runner = CliRunner()


@pytest.fixture()
def sampy_home(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    return tmp_path


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip()


def test_paths_respects_env(sampy_home) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(app, ["paths"])
    assert result.exit_code == 0
    assert str(sampy_home) in result.stdout


def test_config_show_empty_when_missing(sampy_home) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data == {
        "channels": {},
        "providers": {},
        "agents": {},
        "clawhub": None,
    }


def test_config_validate_ok_when_missing(sampy_home) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 0
    assert "ok" in result.stdout


def test_config_validate_fails_on_bad_yaml(sampy_home) -> None:  # type: ignore[no-untyped-def]
    cfg_file = sampy_home / "config.yaml"
    cfg_file.write_text("- this is a list, not a mapping\n")
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 1


def test_config_show_reads_written_file(sampy_home) -> None:  # type: ignore[no-untyped-def]
    cfg_file = sampy_home / "config.yaml"
    cfg_file.write_text("channels:\n  dashboard:\n    dm_policy: open\n    allow_from: [u1]\n")
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["channels"]["dashboard"]["dm_policy"] == "open"


def test_config_path(sampy_home) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(app, ["config", "path"])
    assert result.exit_code == 0
    assert str(sampy_home / "config.yaml") in result.stdout


def test_root_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for sub in ("config", "gateway", "message", "version", "paths"):
        assert sub in result.stdout


def test_message_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["message", "--help"])
    assert result.exit_code == 0
    assert "send" in result.stdout
    assert "agents" in result.stdout


def test_gateway_help_lists_start() -> None:
    result = runner.invoke(app, ["gateway", "--help"])
    assert result.exit_code == 0
    assert "start" in result.stdout
