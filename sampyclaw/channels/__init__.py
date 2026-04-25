"""Core channel abstraction (internal). Plugins use sampyclaw.plugin_sdk, not this. Port of openclaw src/channels/*."""

from sampyclaw.channels.router import ChannelRouter
from sampyclaw.channels.runner import ChannelRunner

__all__ = ["ChannelRouter", "ChannelRunner"]
