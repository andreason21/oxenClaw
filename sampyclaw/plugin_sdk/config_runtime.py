"""Config resolution helpers exposed to plugins.

Port of openclaw `src/plugin-sdk/config-runtime.ts`.
"""

from __future__ import annotations

from sampyclaw.plugin_sdk.config_schema import ChannelConfig, DmPolicy, RootConfig


def resolve_channel_config(root: RootConfig, channel_id: str) -> ChannelConfig | None:
    return root.channels.get(channel_id)


def resolve_dm_policy(root: RootConfig, channel_id: str) -> DmPolicy:
    cfg = root.channels.get(channel_id)
    return cfg.dm_policy if cfg else "pairing"


def resolve_allowed_sender_ids(root: RootConfig, channel_id: str) -> set[str]:
    cfg = root.channels.get(channel_id)
    if cfg is None:
        return set()
    return set(cfg.allow_from)
