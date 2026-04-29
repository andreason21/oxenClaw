"""Tests for the `flows` subsystem (doctor + types + provider + model picker)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.flows import (
    DoctorReport,
    FlowContribution,
    FlowOption,
    FlowOptionGroup,
    list_provider_flow_contributions,
    pick_model_interactively,
    run_doctor,
    sort_flow_contributions_by_label,
)

# ─── types.py ────────────────────────────────────────────────────────


def test_sort_flow_contributions_by_label_is_case_insensitive() -> None:
    items = [
        FlowContribution(
            id=f"id-{i}",
            kind="provider",
            surface="setup",
            option=FlowOption(value=f"v{i}", label=label),
        )
        for i, label in enumerate(["bedrock", "Anthropic", "ollama"])
    ]
    out = sort_flow_contributions_by_label(items)
    assert [c.option.label for c in out] == ["Anthropic", "bedrock", "ollama"]


def test_flow_option_group_is_optional() -> None:
    o = FlowOption(value="x", label="X")
    assert o.group is None


def test_flow_option_group_carries_hint() -> None:
    g = FlowOptionGroup(id="local", label="Local", hint="On-host servers")
    assert g.hint == "On-host servers"


# ─── provider_flow.py ────────────────────────────────────────────────


def test_provider_flow_contributions_cover_all_catalog_providers() -> None:
    from oxenclaw.agents.factory import CATALOG_PROVIDERS

    contribs = list_provider_flow_contributions()
    values = {c.option.value for c in contribs}
    assert values == set(CATALOG_PROVIDERS)


def test_provider_flow_contributions_carry_default_model_metadata() -> None:
    contribs = {c.option.value: c for c in list_provider_flow_contributions()}
    ollama = contribs["ollama"]
    assert ollama.metadata["default_model"] == "qwen3.5:9b"
    assert "default model:" in (ollama.option.hint or "")


def test_local_providers_are_grouped_under_local() -> None:
    contribs = {c.option.value: c for c in list_provider_flow_contributions()}
    for inline in ("ollama", "llamacpp-direct", "vllm", "lmstudio", "llamacpp"):
        assert contribs[inline].option.group is not None
        assert contribs[inline].option.group.id == "local"  # type: ignore[union-attr]


# ─── model_picker.py ────────────────────────────────────────────────


class _StubPrompter:
    """Replays a fixed answer script. Each call to `select` / `text` /
    `confirm` pops the next entry from the matching script."""

    def __init__(
        self,
        *,
        selects: list[str] | None = None,
        texts: list[str] | None = None,
        confirms: list[bool] | None = None,
    ) -> None:
        self._selects = list(selects or [])
        self._texts = list(texts or [])
        self._confirms = list(confirms or [])

    def select(self, message: str, choices: list[str], *, default: str | None = None) -> str:
        return self._selects.pop(0)

    def text(self, message: str, *, default: str | None = None, secret: bool = False) -> str:
        return self._texts.pop(0)

    def confirm(self, message: str, *, default: bool = True) -> bool:
        return self._confirms.pop(0)


def test_pick_model_inline_provider_with_default_model_no_overrides() -> None:
    """Ollama + accept default model + no base_url override.
    Inline providers don't ask for an API key."""
    prompter = _StubPrompter(
        selects=["ollama"],
        confirms=[True, False],  # accept default model, no base_url override
    )
    choice = pick_model_interactively(prompter)
    assert choice.provider == "ollama"
    assert choice.model == "qwen3.5:9b"
    assert choice.base_url is None
    assert choice.api_key is None


def test_pick_model_llamacpp_direct_no_base_url_prompt() -> None:
    """llamacpp-direct: managed-server path — no base_url override
    prompt (oxenClaw picks an ephemeral port itself) and no api_key
    prompt (the catalog has no hosted providers)."""
    prompter = _StubPrompter(
        selects=["llamacpp-direct"],
        confirms=[True],  # accept default model — no override / no key prompts follow
    )
    choice = pick_model_interactively(prompter)
    assert choice.provider == "llamacpp-direct"
    assert choice.model == "local-gguf"
    assert choice.base_url is None
    assert choice.api_key is None


def test_pick_model_inline_provider_with_base_url_override() -> None:
    prompter = _StubPrompter(
        selects=["vllm"],
        confirms=[True, True],  # accept default model, override base_url
        texts=["http://gpu.lan:8000/v1"],
    )
    choice = pick_model_interactively(prompter)
    assert choice.provider == "vllm"
    assert choice.base_url == "http://gpu.lan:8000/v1"


def test_pick_model_user_supplies_custom_model() -> None:
    prompter = _StubPrompter(
        selects=["openai"],
        confirms=[False],  # don't accept default model
        texts=["gpt-4o", ""],  # custom model + skip api key
    )
    choice = pick_model_interactively(prompter)
    assert choice.model == "gpt-4o"
    assert choice.api_key is None


# ─── doctor.py ───────────────────────────────────────────────────────


def _fresh_paths(tmp_path: Path) -> OxenclawPaths:
    p = OxenclawPaths(home=tmp_path)
    p.ensure_home()
    return p


def test_doctor_returns_a_report_object(tmp_path: Path) -> None:
    report = run_doctor(_fresh_paths(tmp_path), probe_embeddings=False)
    assert isinstance(report, DoctorReport)
    assert report.findings  # at least the path/config/etc. probes


def test_doctor_empty_home_is_ok(tmp_path: Path) -> None:
    report = run_doctor(_fresh_paths(tmp_path), probe_embeddings=False)
    assert report.ok  # warnings allowed, no errors


def test_doctor_flags_malformed_config_yaml_as_error(tmp_path: Path) -> None:
    paths = _fresh_paths(tmp_path)
    paths.config_file.write_text(": not yaml :")
    report = run_doctor(paths, probe_embeddings=False)
    assert any(f.area == "config" and f.severity == "error" for f in report.findings)


def test_doctor_provider_drift_is_caught() -> None:
    """Locked invariant — `factory.CATALOG_PROVIDERS` must equal what
    `pi.streaming._PROVIDER_STREAMS` registers. The doctor surfaces
    this as a hard error if drift is introduced."""
    report = run_doctor(probe_embeddings=False)
    providers_finding = next(f for f in report.findings if f.area == "providers")
    assert providers_finding.severity == "ok"


def test_doctor_context_engine_probe_returns_legacy(tmp_path: Path) -> None:
    report = run_doctor(_fresh_paths(tmp_path), probe_embeddings=False)
    ce = next(f for f in report.findings if f.area == "context-engine")
    assert ce.severity == "ok"
    assert "legacy" in (ce.detail or "")


# ─── CLI integration ────────────────────────────────────────────────


def test_oxenclaw_doctor_cli_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    from oxenclaw.cli.__main__ import app

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--skip-embeddings"])
    assert result.exit_code == 0, result.output
    assert "providers" in result.output


def test_oxenclaw_doctor_cli_json_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    from oxenclaw.cli.__main__ import app

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--skip-embeddings", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert isinstance(data["findings"], list)


def test_oxenclaw_setup_provider_cli_lists_inline_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from oxenclaw.cli.__main__ import app

    runner = CliRunner()
    result = runner.invoke(app, ["setup", "provider", "ollama"])
    assert result.exit_code == 0, result.output
    assert "inline" in result.output
    assert "11434" in result.output


def test_oxenclaw_setup_provider_cli_unknown_provider_errors() -> None:
    from oxenclaw.cli.__main__ import app

    runner = CliRunner()
    result = runner.invoke(app, ["setup", "provider", "definitely-not-a-provider"])
    assert result.exit_code == 1
