"""Public plugin SDK. Plugins must import only from this package.

Mirrors openclaw `src/plugin-sdk/*` contracts.
"""

from sampyclaw.plugin_sdk.channel_contract import (
    ChannelPlugin,
    InboundEnvelope,
    MonitorOpts,
    ProbeOpts,
    ProbeResult,
    SendParams,
    SendResult,
)
from sampyclaw.plugin_sdk.error_runtime import (
    ChannelError,
    NetworkError,
    RateLimitedError,
    UserVisibleError,
)
from sampyclaw.plugin_sdk.runtime_env import RuntimeEnv, get_logger

__all__ = [
    "ChannelError",
    "ChannelPlugin",
    "InboundEnvelope",
    "MonitorOpts",
    "NetworkError",
    "ProbeOpts",
    "ProbeResult",
    "RateLimitedError",
    "RuntimeEnv",
    "SendParams",
    "SendResult",
    "UserVisibleError",
    "get_logger",
]
