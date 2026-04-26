"""Bundled Slack plugin entry point — outbound-only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.extensions.slack.accounts import SlackAccountRegistry
from oxenclaw.extensions.slack.channel import SlackChannel
from oxenclaw.plugin_sdk.channel_contract import ChannelPlugin
from oxenclaw.plugin_sdk.config_schema import RootConfig
from oxenclaw.plugins.manifest import Manifest
from oxenclaw.plugins.registry import PluginEntry

_MANIFEST_PATH = Path(__file__).with_name("manifest.json")


def _factory(**kwargs: Any) -> ChannelPlugin:
    account_id = kwargs.pop("account_id", "main")
    token = kwargs.pop("token", None)
    if not token:
        raise ValueError("slack plugin factory requires `token`")
    return SlackChannel(token=token, account_id=account_id, **kwargs)


def _loader(config: RootConfig, paths: OxenclawPaths) -> dict[str, ChannelPlugin]:
    """Bulk-load every Slack account declared in config."""
    registry = SlackAccountRegistry(paths=paths)
    registry.load_from_config(config)
    return {aid: registry.require(aid) for aid in registry.ids()}


SLACK_PLUGIN = PluginEntry(
    manifest=Manifest.from_path(_MANIFEST_PATH),
    factory=_factory,
    loader=_loader,
)
