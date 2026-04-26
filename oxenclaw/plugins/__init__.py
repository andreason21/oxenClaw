"""Plugin discovery, manifest parsing, registry. Port of openclaw src/plugins/*."""

from oxenclaw.plugins.discovery import ENTRY_POINT_GROUP, discover_plugins
from oxenclaw.plugins.manifest import Manifest
from oxenclaw.plugins.registry import PluginEntry, PluginRegistry

__all__ = [
    "ENTRY_POINT_GROUP",
    "Manifest",
    "PluginEntry",
    "PluginRegistry",
    "discover_plugins",
]
