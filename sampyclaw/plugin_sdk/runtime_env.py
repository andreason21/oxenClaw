"""Runtime environment: logger + env helpers exposed to plugins.

Port of openclaw `src/plugin-sdk/runtime-env.ts` and `runtime-logger.ts`.
"""

from __future__ import annotations

import logging
import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path


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


def is_wsl() -> bool:
    """Detect WSL (Windows Subsystem for Linux).

    Returns True only when running inside a real WSL kernel — not when
    running on plain Linux that happens to have "microsoft" in any string.
    Detection follows Microsoft's recommended check: the kernel release
    contains "microsoft" or "WSL".
    """
    if sys.platform != "linux":
        return False
    release = platform.release().lower()
    if "microsoft" in release or "wsl" in release:
        return True
    # Fallback: /proc/sys/kernel/osrelease — same source, more reliable
    # in containers that copy /etc/os-release without /proc.
    osrelease = Path("/proc/sys/kernel/osrelease")
    if osrelease.exists():
        try:
            text = osrelease.read_text(errors="replace").lower()
            return "microsoft" in text or "wsl" in text
        except OSError:
            pass
    return False


def describe_platform() -> str:
    """One-line platform description suitable for the startup banner."""
    base = f"{platform.system()} {platform.release()}"
    if is_wsl():
        return f"{base} (WSL2)"
    return base
