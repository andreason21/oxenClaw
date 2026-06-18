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
    prompt (it is an inline/local provider)."""
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
        confirms=[False, False],  # don't accept default model, no base_url override
        texts=["gpt-4o", ""],  # custom model + empty api key (read from env later)
    )
    choice = pick_model_interactively(prompter)
    assert choice.provider == "openai"
    assert choice.model == "gpt-4o"
    assert choice.base_url is None
    assert choice.api_key is None


def test_pick_model_hosted_provider_persists_api_key() -> None:
    """openai: hosted provider with a bundled default base URL — accept the
    default model, decline the base_url override, and supply a key."""
    prompter = _StubPrompter(
        selects=["openai"],
        confirms=[True, False],  # accept default model, no base_url override
        texts=["sk-test"],  # api key
    )
    choice = pick_model_interactively(prompter)
    assert choice.provider == "openai"
    assert choice.model == "gpt-4o-mini"
    assert choice.base_url is None
    assert choice.api_key == "sk-test"


def test_pick_model_azure_requires_base_url() -> None:
    """azure-openai has no bundled default endpoint, so the base URL is
    prompted unconditionally (no confirm) before the API key."""
    prompter = _StubPrompter(
        selects=["azure-openai"],
        confirms=[True],  # accept default model
        texts=["https://res.openai.azure.com", "az-key"],  # base_url (required) + api key
    )
    choice = pick_model_interactively(prompter)
    assert choice.provider == "azure-openai"
    assert choice.base_url == "https://res.openai.azure.com"
    assert choice.api_key == "az-key"


# ─── provider_config.py ──────────────────────────────────────────────


def test_configure_hosted_provider_persists_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    from oxenclaw.flows.provider_config import configure_hosted_provider

    emitted: list[str] = []
    prompter = _StubPrompter(texts=["", "sk-test"])  # default model, api key
    result = configure_hosted_provider("openai", prompter=prompter, emit=emitted.append)

    assert result.provider == "openai"
    assert result.model == "gpt-4o-mini"
    assert result.base_url is None
    assert result.api_key_saved is True
    assert 'export OPENAI_API_KEY="sk-test"' in (tmp_path / "env").read_text(encoding="utf-8")
    assert any("gateway start --provider openai" in m for m in emitted)


def test_configure_hosted_provider_azure_prompts_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    from oxenclaw.flows.provider_config import configure_hosted_provider

    prompter = _StubPrompter(texts=["", "https://res.openai.azure.com", "az-key"])
    result = configure_hosted_provider("azure-openai", prompter=prompter, emit=lambda _m: None)

    assert result.base_url == "https://res.openai.azure.com"
    assert result.api_key_saved is True
    assert 'export AZURE_OPENAI_API_KEY="az-key"' in (tmp_path / "env").read_text(encoding="utf-8")


def test_configure_hosted_provider_empty_key_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    from oxenclaw.flows.provider_config import configure_hosted_provider

    emitted: list[str] = []
    prompter = _StubPrompter(texts=["", ""])  # default model, empty key
    result = configure_hosted_provider("gemini", prompter=prompter, emit=emitted.append)

    assert result.api_key_saved is False
    assert not (tmp_path / "env").exists()
    assert any("export GEMINI_API_KEY" in m for m in emitted)


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


def _clear_hosted_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OPENAI_API_KEY", "GEMINI_API_KEY", "AZURE_OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_doctor_provider_auth_ok_with_no_hosted_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local-first default: no hosted key is a clean state, not an error."""
    _clear_hosted_keys(monkeypatch)
    report = run_doctor(_fresh_paths(tmp_path), probe_embeddings=False)
    pa = next(f for f in report.findings if f.area == "provider-auth")
    assert pa.severity == "ok"
    assert "local-first" in pa.message


def test_doctor_provider_auth_reports_configured_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key in the environment (as `setup provider` persists) shows up OK."""
    _clear_hosted_keys(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-xyz")
    report = run_doctor(_fresh_paths(tmp_path), probe_embeddings=False)
    pa = next(f for f in report.findings if f.area == "provider-auth")
    assert pa.severity == "ok"
    assert "OPENAI_API_KEY set" in (pa.detail or "")


def test_doctor_provider_auth_errors_when_config_pins_hosted_without_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_hosted_keys(monkeypatch)
    paths = _fresh_paths(tmp_path)
    paths.config_file.write_text("agents:\n  default:\n    provider: openai\n", encoding="utf-8")
    report = run_doctor(paths, probe_embeddings=False)
    pa = next(f for f in report.findings if f.area == "provider-auth")
    assert pa.severity == "error"
    assert "no API key" in pa.message
    assert report.ok is False


def test_doctor_provider_auth_errors_on_azure_without_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_hosted_keys(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-key")
    paths = _fresh_paths(tmp_path)
    paths.config_file.write_text(
        "agents:\n  default:\n    provider: azure-openai\n", encoding="utf-8"
    )
    report = run_doctor(paths, probe_embeddings=False)
    pa = next(f for f in report.findings if f.area == "provider-auth")
    assert pa.severity == "error"
    assert "base_url" in pa.message


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


def test_oxenclaw_setup_provider_cli_hosted_show_does_not_persist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--show` (and any non-tty run) reports requirements without writing."""
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    from oxenclaw.cli.__main__ import app

    runner = CliRunner()
    result = runner.invoke(app, ["setup", "provider", "openai", "--show"])
    assert result.exit_code == 0, result.output
    assert "hosted" in result.output
    assert "OPENAI_API_KEY" in result.output
    assert not (tmp_path / "env").exists()
