"""Tests for models.dev integration."""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from oxenclaw.pi import models_dev
from oxenclaw.pi.registry import (
    InMemoryModelRegistry,
    RemoteModelRegistry,
    guess_provider_from_id,
)


@pytest.fixture(autouse=True)
def _reset_caches(tmp_path, monkeypatch):
    """Wipe in-memory + redirect disk cache to a tmp dir per test."""
    monkeypatch.setattr(models_dev, "DISK_CACHE_PATH", tmp_path / "cache.json")
    models_dev.reset_cache_for_tests()
    yield
    models_dev.reset_cache_for_tests()


def test_snapshot_loads_when_no_network() -> None:
    with patch.object(models_dev, "_fetch_network", return_value=None):
        data = models_dev.fetch_models_dev()
    # Snapshot has the curated providers we shipped.
    assert "anthropic" in data
    assert "openai" in data
    assert "google" in data


def test_get_model_capabilities_extraction() -> None:
    data = {
        "anthropic": {
            "models": {
                "claude-opus-4-7": {
                    "tool_call": True,
                    "attachment": True,
                    "reasoning": True,
                    "family": "claude",
                    "limit": {"context": 200000, "output": 32000},
                }
            }
        }
    }
    caps = models_dev.get_model_capabilities("claude-opus-4-7", data)
    assert caps["context_window"] == 200000
    assert caps["max_output"] == 32000
    assert caps["supports_tools"] is True
    assert caps["supports_attachments"] is True
    assert caps["supports_reasoning"] is True
    assert caps["family"] == "claude"
    assert caps["provider"] == "anthropic"


def test_get_model_capabilities_unknown_model() -> None:
    caps = models_dev.get_model_capabilities("does-not-exist", {})
    assert caps["context_window"] is None
    assert caps["max_output"] is None
    assert caps["supports_tools"] is True  # lenient default
    assert caps["provider"] is None


def test_get_model_capabilities_image_modality_implies_attachment() -> None:
    data = {
        "google": {
            "models": {
                "gemini-x": {
                    "modalities": {"input": ["text", "image"]},
                    "limit": {"context": 1000000, "output": 8192},
                }
            }
        }
    }
    caps = models_dev.get_model_capabilities("gemini-x", data)
    assert caps["supports_attachments"] is True


def test_in_memory_cache_avoids_repeat_fetch() -> None:
    fake = {"openai": {"models": {"gpt-5": {"limit": {"context": 400000, "output": 128000}}}}}
    with patch.object(models_dev, "_fetch_network", return_value=fake) as net:
        models_dev.fetch_models_dev()
        models_dev.fetch_models_dev()
        models_dev.fetch_models_dev()
    assert net.call_count == 1


def test_disk_cache_used_on_network_failure(tmp_path) -> None:
    # Pre-populate disk cache.
    cache_path = models_dev.DISK_CACHE_PATH
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"_fetched_at": time.time(), "data": {"openai": {"models": {}}}})
    )
    with patch.object(models_dev, "_fetch_network", return_value=None):
        data = models_dev.fetch_models_dev()
    assert "openai" in data


def test_stale_disk_cache_then_snapshot_when_network_down(tmp_path) -> None:
    # Stale disk cache: timestamp before TTL, but data present.
    cache_path = models_dev.DISK_CACHE_PATH
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    stale_data = {"only-here": {"models": {}}}
    cache_path.write_text(json.dumps({"_fetched_at": time.time() - 10 * 3600, "data": stale_data}))
    with patch.object(models_dev, "_fetch_network", return_value=None):
        data = models_dev.fetch_models_dev()
    # Stale disk cache takes precedence over snapshot (data is non-empty).
    assert data == stale_data


def test_remote_registry_falls_back_to_static_catalog() -> None:
    from oxenclaw.pi.models import Model

    seed = Model(id="claude-opus-4-7", provider="anthropic", context_window=200000)
    reg = RemoteModelRegistry(models=[seed])
    # Static catalog hits short-circuit any models.dev fetch.
    with patch.object(models_dev, "fetch_models_dev") as net:
        m = reg.require("claude-opus-4-7")
    assert m.id == "claude-opus-4-7"
    assert net.call_count == 0


def test_remote_registry_resolves_unknown_model_via_models_dev() -> None:
    reg = RemoteModelRegistry()
    fake = {
        "openai": {
            "models": {
                "gpt-5": {
                    "tool_call": True,
                    "attachment": True,
                    "reasoning": True,
                    "limit": {"context": 400000, "output": 128000},
                }
            }
        }
    }
    with patch.object(models_dev, "fetch_models_dev", return_value=fake):
        m = reg.require("gpt-5")
    assert m.id == "gpt-5"
    assert m.provider == "openai"
    assert m.context_window == 400000
    assert m.max_output_tokens == 128000


def test_remote_registry_unknown_model_falls_back_to_probe_tier() -> None:
    reg = RemoteModelRegistry()
    with patch.object(models_dev, "fetch_models_dev", return_value={}):
        m = reg.require("totally-new-model-2099")
    assert m.context_window == models_dev.CONTEXT_PROBE_TIERS[0]
    # Catalog is on-host only — provider-guess fallback is `ollama`.
    assert m.provider == "ollama"


def test_models_dev_enabled_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("OXENCLAW_USE_MODELS_DEV", "1")
    assert models_dev.models_dev_enabled() is True
    monkeypatch.setenv("OXENCLAW_USE_MODELS_DEV", "0")
    assert models_dev.models_dev_enabled() is False
    monkeypatch.delenv("OXENCLAW_USE_MODELS_DEV", raising=False)
    assert models_dev.models_dev_enabled() is False


def test_default_registry_picks_remote_when_env_flag_set(monkeypatch) -> None:
    from oxenclaw.pi.catalog import default_registry

    monkeypatch.setenv("OXENCLAW_USE_MODELS_DEV", "1")
    reg = default_registry()
    assert isinstance(reg, RemoteModelRegistry)
    monkeypatch.delenv("OXENCLAW_USE_MODELS_DEV")
    reg2 = default_registry()
    assert isinstance(reg2, InMemoryModelRegistry)
    assert not isinstance(reg2, RemoteModelRegistry)


def test_guess_provider_from_id() -> None:
    # Catalog is on-host only: every prefix lands on `ollama`.
    assert guess_provider_from_id("qwen3.5:9b") == "ollama"
    assert guess_provider_from_id("llama3.1:8b") == "ollama"
    assert guess_provider_from_id("gemma4:e4b") == "ollama"
    assert guess_provider_from_id("mistral-nemo:12b") == "ollama"
    # Unknown prefix falls back to ollama too.
    assert guess_provider_from_id("totally-new-2099") == "ollama"


def test_lookup_models_dev_context_uses_cascade() -> None:
    fake = {"openai": {"models": {"gpt-5": {"limit": {"context": 400000, "output": 128000}}}}}
    with patch.object(models_dev, "_fetch_network", return_value=fake):
        ctx = models_dev.lookup_models_dev_context("gpt-5")
    assert ctx == 400000
