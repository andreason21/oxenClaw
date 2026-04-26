"""Tests for Manifest (Pydantic model) + Telegram bundled manifest.json."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from sampyclaw.plugins.manifest import Manifest


def test_manifest_from_dict() -> None:
    m = Manifest.model_validate(
        {
            "id": "telegram",
            "name": "Telegram",
            "channels": ["telegram"],
            "channelEnvVars": {"telegram": ["TELEGRAM_BOT_TOKEN"]},
            "configSchema": {"type": "object"},
        }
    )
    assert m.id == "telegram"
    assert m.channels == ["telegram"]
    assert m.channel_env_vars == {"telegram": ["TELEGRAM_BOT_TOKEN"]}
    assert m.config_schema == {"type": "object"}


def test_manifest_from_json_string() -> None:
    m = Manifest.from_json('{"id": "x", "name": "X", "channels": ["x"]}')
    assert m.id == "x"


def test_manifest_from_path(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"id": "x", "channels": ["x"]}))
    m = Manifest.from_path(path)
    assert m.id == "x"


def test_manifest_requires_id() -> None:
    with pytest.raises(ValidationError):
        Manifest.model_validate({"channels": ["x"]})


def test_bundled_telegram_manifest_parses() -> None:
    from sampyclaw.extensions.telegram.plugin_entry import TELEGRAM_PLUGIN

    assert TELEGRAM_PLUGIN.manifest.id == "telegram"
    assert "telegram" in TELEGRAM_PLUGIN.manifest.channels
    assert "TELEGRAM_BOT_TOKEN" in TELEGRAM_PLUGIN.manifest.channel_env_vars["telegram"]


def test_manifest_preserves_extra_keys() -> None:
    m = Manifest.model_validate({"id": "x", "customField": 1})
    dumped = m.model_dump()
    assert dumped.get("customField") == 1
