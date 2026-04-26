"""Bundled Slack plugin entry point — outbound-only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sampyclaw.config.paths import SampyclawPaths
from sampyclaw.extensions.slack.accounts import SlackAccountRegistry
from sampyclaw.extensions.slack.channel import SlackChannel
from sampyclaw.plugin_sdk.channel_contract import ChannelPlugin
from sampyclaw.plugin_sdk.config_schema import RootConfig
from sampyclaw.plugins.manifest import Manifest
from sampyclaw.plugins.registry import PluginEntry

_MANIFEST_PATH = Path(__file__).with_name("manifest.json")


def _factory(**kwargs: Any) -> ChannelPlugin:
    account_id = kwargs.pop("account_id", "main")
    token = kwargs.pop("token", None)
    if not token:
        raise ValueError("slack plugin factory requires `token`")
    return SlackChannel(token=token, account_id=account_id, **kwargs)


def _loader(
    config: RootConfig, paths: SampyclawPaths
) -> dict[str, ChannelPlugin]:
    """Bulk-load every Slack account declared in config."""
    registry = SlackAccountRegistry(paths=paths)
    registry.load_from_config(config)
    return {aid: registry.require(aid) for aid in registry.ids()}


SLACK_PLUGIN = PluginEntry(
    manifest=Manifest.from_path(_MANIFEST_PATH),
    factory=_factory,
    loader=_loader,
)
