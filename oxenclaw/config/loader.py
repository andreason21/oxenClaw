"""YAML config loader with env var substitution and Pydantic validation.

Port of openclaw `src/config/load.ts`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from oxenclaw.config.env_subst import substitute
from oxenclaw.config.paths import OxenclawPaths, default_paths
from oxenclaw.plugin_sdk.config_schema import RootConfig


class ConfigError(Exception):
    """Raised when config is missing, malformed, or fails validation."""


def load_config(paths: OxenclawPaths | None = None) -> RootConfig:
    """Load, substitute env, and validate the root config. Missing file → empty config."""
    resolved = paths or default_paths()
    if not resolved.config_file.exists():
        return RootConfig()
    return _parse(resolved.config_file)


def load_config_from_text(text: str) -> RootConfig:
    """Parse config YAML from a string. Primarily for tests."""
    raw = yaml.safe_load(text) or {}
    return _validate(substitute(raw))


def _parse(path: Path) -> RootConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"failed to parse {path}: {exc}") from exc
    return _validate(substitute(raw))


def _validate(data: Any) -> RootConfig:
    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a mapping, got {type(data).__name__}")
    try:
        return RootConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"config validation failed:\n{exc}") from exc
