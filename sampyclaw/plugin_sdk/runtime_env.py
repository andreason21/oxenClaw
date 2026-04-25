"""Runtime environment: logger + env helpers exposed to plugins.

Port of openclaw `src/plugin-sdk/runtime-env.ts` and `runtime-logger.ts`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass


def get_logger(name: str) -> logging.Logger:
    """Get a namespaced logger. Plugins call this instead of `logging.getLogger`.

    Matches openclaw's convention where all plugin logs flow through the
    SDK-provided logger so the core can redirect/format them.
    """
    return logging.getLogger(f"sampyclaw.{name}")


@dataclass(frozen=True)
class RuntimeEnv:
    """Subset of process env exposed to plugins. Explicit allowlist preferred over raw os.environ."""

    home_dir: str
    config_dir: str

    def get(self, key: str, default: str | None = None) -> str | None:
        return os.environ.get(key, default)


def default_runtime_env() -> RuntimeEnv:
    home = os.path.expanduser("~")
    cfg = os.environ.get("SAMPYCLAW_HOME", os.path.join(home, ".sampyclaw"))
    return RuntimeEnv(home_dir=home, config_dir=cfg)
