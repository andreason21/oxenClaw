"""Tests for config loader, env substitution, and credential store."""

from __future__ import annotations

import pytest

from oxenclaw.config import (
    ConfigError,
    CredentialStore,
    OxenclawPaths,
    load_config,
    load_config_from_text,
)
from oxenclaw.config.env_subst import MissingEnvVar, substitute


def test_env_subst_braced_and_bare() -> None:
    env = {"FOO": "bar", "BAZ": "qux"}
    assert substitute("$FOO/${BAZ}", env) == "bar/qux"


def test_env_subst_default_fallback() -> None:
    assert substitute("${MISSING:-fallback}", {}) == "fallback"


def test_env_subst_missing_raises() -> None:
    with pytest.raises(MissingEnvVar):
        substitute("$UNSET", {})


def test_env_subst_walks_nested() -> None:
    out = substitute({"a": ["$X", {"b": "$Y"}]}, {"X": "1", "Y": "2"})
    assert out == {"a": ["1", {"b": "2"}]}


def test_load_config_from_text_validates() -> None:
    cfg = load_config_from_text(
        """
        channels:
          dashboard:
            accounts:
              - account_id: main
                display_name: Bot
            dm_policy: open
            allow_from:
              - "user-1"
        agents:
          assistant:
            id: assistant
            provider: anthropic
        """
    )
    assert "dashboard" in cfg.channels
    assert cfg.channels["dashboard"].dm_policy == "open"
    assert cfg.channels["dashboard"].allow_from == ["user-1"]
    assert cfg.agents["assistant"].provider == "anthropic"


def test_load_config_from_text_rejects_bad_root() -> None:
    with pytest.raises(ConfigError):
        load_config_from_text("- just-a-list")


def test_load_config_missing_file_is_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    paths = OxenclawPaths(home=tmp_path)
    cfg = load_config(paths)
    assert cfg.channels == {}
    assert cfg.agents == {}


def test_credential_store_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    store = CredentialStore(paths)

    assert store.read("dashboard", "main") is None

    store.write("dashboard", "main", {"token": "abc123"})
    assert store.read("dashboard", "main") == {"token": "abc123"}
    assert store.list_accounts("dashboard") == ["main"]

    store.write("dashboard", "secondary", {"token": "def456"})
    assert store.list_accounts("dashboard") == ["main", "secondary"]

    assert store.delete("dashboard", "main") is True
    assert store.delete("dashboard", "main") is False
    assert store.list_accounts("dashboard") == ["secondary"]


def test_credential_file_permissions(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import stat

    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    store = CredentialStore(paths)
    store.write("dashboard", "main", {"token": "secret"})
    mode = paths.credential_file("dashboard", "main").stat().st_mode
    assert stat.S_IMODE(mode) == 0o600
