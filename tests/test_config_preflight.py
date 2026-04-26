"""Tests for `oxenclaw.config.preflight`."""

from __future__ import annotations

import json
from pathlib import Path

from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.config.preflight import run_preflight


def _paths(tmp_path: Path) -> OxenclawPaths:
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    return paths


def test_preflight_ok_for_empty_home(tmp_path: Path):
    paths = _paths(tmp_path)
    report = run_preflight(paths, probe_embeddings=False)
    assert report.ok
    assert report.errors == []


def test_preflight_flags_malformed_config_yaml(tmp_path: Path):
    paths = _paths(tmp_path)
    paths.config_file.write_text(": not yaml :")
    report = run_preflight(paths, probe_embeddings=False)
    assert not report.ok
    assert any("config" in f.source for f in report.errors)


def test_preflight_flags_malformed_mcp_json(tmp_path: Path):
    paths = _paths(tmp_path)
    paths.mcp_config_file.write_text("not json {")
    report = run_preflight(paths, probe_embeddings=False)
    assert not report.ok
    assert any("mcp.json" in f.source for f in report.errors)


def test_preflight_warns_on_missing_env_ref(tmp_path: Path, monkeypatch):
    paths = _paths(tmp_path)
    paths.mcp_config_file.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {
                        "url": "https://mcp.example.com/sse",
                        "headers": {"Authorization": "Bearer ${UNSET_TOKEN_X}"},
                    }
                }
            }
        )
    )
    monkeypatch.delenv("UNSET_TOKEN_X", raising=False)
    report = run_preflight(paths, probe_embeddings=False)
    # Missing env ref is a warning, not an error.
    assert report.ok
    assert any("UNSET_TOKEN_X" in f.message for f in report.warnings)


def test_preflight_no_warning_when_env_set(tmp_path: Path, monkeypatch):
    paths = _paths(tmp_path)
    paths.mcp_config_file.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {
                        "url": "https://mcp.example.com/sse",
                        "headers": {"Authorization": "Bearer ${SET_TOKEN_X}"},
                    }
                }
            }
        )
    )
    monkeypatch.setenv("SET_TOKEN_X", "tok-abc")
    report = run_preflight(paths, probe_embeddings=False)
    missing_warnings = [f for f in report.warnings if "SET_TOKEN_X" in f.message]
    assert missing_warnings == []


def test_preflight_flags_malformed_credentials(tmp_path: Path):
    paths = _paths(tmp_path)
    cred_dir = paths.credentials_dir / "telegram"
    cred_dir.mkdir(parents=True)
    (cred_dir / "main.json").write_text("{not json")
    report = run_preflight(paths, probe_embeddings=False)
    assert not report.ok
    assert any("main.json" in f.source for f in report.errors)


def test_preflight_finding_format_is_human_readable():
    from oxenclaw.config.preflight import PreflightFinding

    f = PreflightFinding(severity="error", source="config.yaml", message="missing 'channels'")
    assert f.format() == "[error] config.yaml: missing 'channels'"
