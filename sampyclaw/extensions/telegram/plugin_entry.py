"""Bundled Telegram plugin entry point.

Shape: third-party plugins provide one of these at their entry-point target.
Loaded lazily via `importlib.metadata.entry_points` during discovery.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sampyclaw.config.paths import SampyclawPaths
from sampyclaw.extensions.telegram.accounts import TelegramAccountRegistry
from sampyclaw.extensions.telegram.channel import TelegramChannel
from sampyclaw.plugin_sdk.channel_contract import ChannelPlugin
from sampyclaw.plugin_sdk.config_schema import RootConfig
from sampyclaw.plugins.manifest import Manifest
from sampyclaw.plugins.registry import PluginEntry

_MANIFEST_PATH = Path(__file__).with_name("manifest.json")


def _factory(**kwargs: Any) -> ChannelPlugin:
    # Manifest declares `channelEnvVars.telegram = ["TELEGRAM_BOT_TOKEN"]` so
    # callers either supply `token=...` directly or resolve via TokenResolver
    # before calling us.
    account_id = kwargs.pop("account_id", "main")
    token = kwargs.pop("token", None)
    if not token:
        raise ValueError("telegram plugin factory requires `token`")
    return TelegramChannel(token=token, account_id=account_id)


def _loader(
    config: RootConfig, paths: SampyclawPaths
) -> dict[str, ChannelPlugin]:
    """Bulk-load every Telegram account declared in config using the credential store."""
    registry = TelegramAccountRegistry(paths=paths)
    registry.load_from_config(config)
    return {aid: registry.require(aid) for aid in registry.ids()}


TELEGRAM_PLUGIN = PluginEntry(
    manifest=Manifest.from_path(_MANIFEST_PATH),
    factory=_factory,
    loader=_loader,
)
