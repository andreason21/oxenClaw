"""Tests for build_agent factory.

Post-rc.15: every catalog provider routes through PiAgent. The pre-pi
LocalAgent / AnthropicAgent class routes were removed from the factory.
Legacy provider names (`local`, `pi`) still work via the alias map but
emit a deprecation warning.
"""

from __future__ import annotations

import logging

import pytest

from oxenclaw.agents import (
    SUPPORTED_PROVIDERS,
    EchoAgent,
    UnknownProvider,
    build_agent,
)
from oxenclaw.agents.factory import (
    CATALOG_PROVIDERS,
    LEGACY_ALIASES,
    PROVIDER_DEFAULT_MODELS,
)
from oxenclaw.agents.pi_agent import PiAgent


def test_supported_providers_is_catalog_plus_echo() -> None:
    assert set(SUPPORTED_PROVIDERS) == set(CATALOG_PROVIDERS) | {"echo"}


def test_catalog_providers_match_pi_registrations() -> None:
    """Every catalog provider id must have a registered stream wrapper.
    The reverse — extra wrappers without a matching catalog id — is
    legitimate (plugin / test stubs), so this is a one-way subset check
    rather than equality."""
    import oxenclaw.pi.providers  # noqa: F401  registers wrappers
    from oxenclaw.pi.streaming import _PROVIDER_STREAMS  # type: ignore[attr-defined]

    missing = set(CATALOG_PROVIDERS) - set(_PROVIDER_STREAMS.keys())
    assert not missing, f"advertised but not wired: {sorted(missing)}"


def test_build_echo_returns_echo_agent() -> None:
    agent = build_agent(agent_id="a", provider="echo")
    assert isinstance(agent, EchoAgent)


def test_build_lmstudio_with_explicit_model() -> None:
    """Explicit model id + system prompt round-trip through PiAgent."""
    agent = build_agent(
        agent_id="a",
        provider="lmstudio",
        system_prompt="You are brief.",
        model="qwen2.5:7b-instruct",
    )
    assert isinstance(agent, PiAgent)
    assert agent._model.id == "qwen2.5:7b-instruct"
    assert agent._system_prompt == "You are brief."


def test_build_ollama_picks_qwen35_default() -> None:
    """openclaw-style: `--provider ollama` lands on PiAgent with the
    catalog's tool-capable qwen3.5:9b default."""
    agent = build_agent(agent_id="a", provider="ollama")
    assert isinstance(agent, PiAgent)
    assert agent._model.id == PROVIDER_DEFAULT_MODELS["ollama"] == "qwen3.5:9b"
    assert agent._model.provider == "ollama"


def test_build_local_alias_falls_back_to_ollama_when_unconfigured(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--provider local` is a legacy alias that now routes through the
    `auto` resolver. With no GGUF / llama-server configured, the resolver
    must fall back to `ollama` so existing Ollama-only installs don't
    break on first run after the default flip."""
    monkeypatch.delenv("OXENCLAW_LLAMACPP_GGUF", raising=False)
    with caplog.at_level(logging.WARNING):
        agent = build_agent(agent_id="a", provider="local")
    assert isinstance(agent, PiAgent)
    assert agent._model.provider == "ollama"
    assert any("legacy alias" in r.getMessage() for r in caplog.records)


def test_build_pi_alias_resolves_via_auto(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OXENCLAW_LLAMACPP_GGUF", raising=False)
    with caplog.at_level(logging.WARNING):
        agent = build_agent(agent_id="a", provider="pi")
    assert isinstance(agent, PiAgent)
    assert agent._model.provider == "ollama"
    assert any("legacy alias" in r.getMessage() for r in caplog.records)


def test_auto_picks_llamacpp_direct_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """When both `$OXENCLAW_LLAMACPP_GGUF` is set and a llama-server
    binary is reachable, `--provider auto` (and the `local`/`pi` aliases
    that route through it) must select `llamacpp-direct`."""
    fake_gguf = tmp_path / "model.gguf"
    fake_gguf.write_bytes(b"\x00")
    fake_bin = tmp_path / "llama-server"
    fake_bin.write_text("#!/bin/sh\nexit 0\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("OXENCLAW_LLAMACPP_GGUF", str(fake_gguf))
    monkeypatch.setenv("OXENCLAW_LLAMACPP_BIN", str(fake_bin))

    agent = build_agent(agent_id="a", provider="auto")
    assert isinstance(agent, PiAgent)
    assert agent._model.provider == "llamacpp-direct"

    # Legacy alias should land in the same place.
    agent2 = build_agent(agent_id="b", provider="local")
    assert agent2._model.provider == "llamacpp-direct"


def test_resolve_default_local_provider_branches(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from oxenclaw.agents.factory import resolve_default_local_provider

    # Branch 1: no GGUF env → ollama.
    monkeypatch.delenv("OXENCLAW_LLAMACPP_GGUF", raising=False)
    assert resolve_default_local_provider() == "ollama"

    # Branch 2: GGUF env set but binary missing → still ollama
    # (we don't pretend to have llama-server when we can't find it).
    monkeypatch.setenv("OXENCLAW_LLAMACPP_GGUF", str(tmp_path / "x.gguf"))
    monkeypatch.setenv("OXENCLAW_LLAMACPP_BIN", str(tmp_path / "no-such-bin"))
    assert resolve_default_local_provider() == "ollama"

    # Branch 3: GGUF env + reachable binary → llamacpp-direct.
    fake_bin = tmp_path / "llama-server"
    fake_bin.write_text("#!/bin/sh\nexit 0\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("OXENCLAW_LLAMACPP_BIN", str(fake_bin))
    assert resolve_default_local_provider() == "llamacpp-direct"


def test_build_vllm_with_custom_model_synthesises_registry_entry() -> None:
    """vLLM users typically run a model that isn't in the catalog
    (`meta-llama/Llama-3.1-8B-Instruct`, fine-tunes, etc.). The factory
    synthesises a transient registry entry instead of failing."""
    agent = build_agent(
        agent_id="a",
        provider="vllm",
        model="meta-llama/Llama-3.1-8B-Instruct",
        base_url="http://internal-vllm.lan:8000/v1",
        api_key="sk-internal",
    )
    assert isinstance(agent, PiAgent)
    assert agent._model.id == "meta-llama/Llama-3.1-8B-Instruct"
    assert agent._model.provider == "vllm"
    assert agent._model.extra.get("base_url") == "http://internal-vllm.lan:8000/v1"


def test_build_with_base_url_override_on_catalog_model() -> None:
    """Catalog model + custom base_url → registry copy with override."""
    agent = build_agent(
        agent_id="a",
        provider="ollama",
        model="gemma4:latest",
        base_url="http://gpu.local:11434/v1",
    )
    assert isinstance(agent, PiAgent)
    assert agent._model.extra.get("base_url") == "http://gpu.local:11434/v1"


def test_unknown_provider_raises() -> None:
    with pytest.raises(UnknownProvider):
        build_agent(agent_id="a", provider="gpt5")


def test_legacy_aliases_cover_pre_rc15_names() -> None:
    """The two main pre-rc.15 names (`local`, `pi`) must remain
    accepted so existing config.yaml + dashboards keep working. They
    now route through the `auto` sentinel so the resolver picks the
    right local backend per host (llamacpp-direct when configured,
    else ollama)."""
    assert "local" in LEGACY_ALIASES
    assert "pi" in LEGACY_ALIASES
    assert LEGACY_ALIASES["local"] == "auto"
    assert LEGACY_ALIASES["pi"] == "auto"


def test_default_tools_are_registered_when_none_passed() -> None:
    """Default agents now ship with the openclaw-style fs/shell/process/
    plan bundle — read/write/edit/grep/glob/list_dir/read_pdf/shell/
    process/update_plan in addition to echo/get_time. Mutating tools
    are raw without an ApprovalManager (operator opts in to gating
    via OXENCLAW_APPROVER_TOKEN); read-only tools are always present."""
    agent = build_agent(agent_id="a", provider="ollama")
    names = set(agent._tools.names())
    # Read-only bundle (always there, never gated).
    for t in ("echo", "get_time", "read_file", "list_dir", "grep", "glob", "read_pdf"):
        assert t in names, f"missing read-only tool {t!r}: {sorted(names)}"
    # Mutating bundle.
    for t in ("write_file", "edit", "shell", "process"):
        assert t in names, f"missing mutating tool {t!r}: {sorted(names)}"
    # Plan tracker (ungated).
    assert "update_plan" in names
