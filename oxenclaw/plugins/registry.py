"""Plugin registry keyed by manifest id.

A `PluginEntry` pairs a `Manifest` (the declarative control-plane data)
with a `factory` callable that builds a channel instance bound to a
specific account. Third-party plugins populate this via entry points;
bundled plugins (telegram) register themselves from `oxenclaw.extensions.*`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from oxenclaw.plugin_sdk.channel_contract import ChannelPlugin
from oxenclaw.plugins.manifest import Manifest

if TYPE_CHECKING:
    from oxenclaw.config.paths import OxenclawPaths
    from oxenclaw.plugin_sdk.config_schema import RootConfig


AccountLoader = Callable[["RootConfig", "OxenclawPaths"], dict[str, ChannelPlugin]]


@dataclass(frozen=True)
class PluginEntry:
    """Describes a loadable channel plugin.

    `factory` — low-level constructor taking kwargs (at minimum `account_id`
    and the channel's credential, e.g. `token`). Used by tests and direct
    callers that already know their account shape.

    `loader` — optional higher-level helper that reads `config` +
    credentials under `paths` and returns `{account_id: ChannelPlugin}` for
    every account this plugin should own. The gateway uses this so bulk
    wiring stays channel-agnostic.
    """

    manifest: Manifest
    factory: Callable[..., ChannelPlugin]
    loader: AccountLoader | None = field(default=None)

    @property
    def id(self) -> str:
        return self.manifest.id

    def create(self, **kwargs: Any) -> ChannelPlugin:
        return self.factory(**kwargs)

    def load_accounts(self, config: RootConfig, paths: OxenclawPaths) -> dict[str, ChannelPlugin]:
        if self.loader is None:
            return {}
        return self.loader(config, paths)


class PluginRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, PluginEntry] = {}

    def register(self, entry: PluginEntry) -> None:
        if entry.id in self._entries:
            raise ValueError(f"duplicate plugin id: {entry.id}")
        self._entries[entry.id] = entry

    def get(self, plugin_id: str) -> PluginEntry | None:
        return self._entries.get(plugin_id)

    def require(self, plugin_id: str) -> PluginEntry:
        entry = self._entries.get(plugin_id)
        if entry is None:
            raise KeyError(f"plugin {plugin_id!r} not registered")
        return entry

    def ids(self) -> list[str]:
        return sorted(self._entries)

    def manifests(self) -> list[Manifest]:
        return [e.manifest for e in self._entries.values()]

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, plugin_id: object) -> bool:
        return plugin_id in self._entries
