"""Plugin discovery, manifest parsing, registry. Port of openclaw src/plugins/*."""

from sampyclaw.plugins.discovery import ENTRY_POINT_GROUP, discover_plugins
from sampyclaw.plugins.manifest import Manifest
from sampyclaw.plugins.registry import PluginEntry, PluginRegistry

__all__ = [
    "ENTRY_POINT_GROUP",
    "Manifest",
    "PluginEntry",
    "PluginRegistry",
    "discover_plugins",
]
