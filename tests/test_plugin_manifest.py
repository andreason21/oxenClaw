"""Tests for Manifest (Pydantic model) + bundled Slack manifest.json."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from oxenclaw.plugins.manifest import Manifest


def test_manifest_from_dict() -> None:
    m = Manifest.model_validate(
        {
            "id": "slack",
            "name": "Slack",
            "channels": ["slack"],
            "channelEnvVars": {"slack": ["SLACK_BOT_TOKEN"]},
            "configSchema": {"type": "object"},
        }
    )
    assert m.id == "slack"
    assert m.channels == ["slack"]
    assert m.channel_env_vars == {"slack": ["SLACK_BOT_TOKEN"]}
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


def test_bundled_slack_manifest_parses() -> None:
    from oxenclaw.extensions.slack.plugin_entry import SLACK_PLUGIN

    assert SLACK_PLUGIN.manifest.id == "slack"
    assert "slack" in SLACK_PLUGIN.manifest.channels


def test_manifest_preserves_extra_keys() -> None:
    m = Manifest.model_validate({"id": "x", "customField": 1})
    dumped = m.model_dump()
    assert dumped.get("customField") == 1
