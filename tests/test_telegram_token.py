"""Tests for TokenResolver: credential store → env fallback → require/rotate/forget."""

from __future__ import annotations

import pytest

from sampyclaw.config.credentials import CredentialStore
from sampyclaw.config.paths import SampyclawPaths
from sampyclaw.extensions.telegram.token import DEFAULT_ENV_KEY, TokenResolver
from sampyclaw.plugin_sdk.error_runtime import UserVisibleError


@pytest.fixture()
def store(tmp_path) -> CredentialStore:  # type: ignore[no-untyped-def]
    paths = SampyclawPaths(home=tmp_path)
    paths.ensure_home()
    return CredentialStore(paths)


def test_resolve_returns_stored_token(store: CredentialStore) -> None:
    store.write("telegram", "main", {"token": "abc"})
    assert TokenResolver(store).resolve("main") == "abc"


def test_resolve_missing_returns_none(store: CredentialStore) -> None:
    assert TokenResolver(store).resolve("absent") is None


def test_resolve_falls_back_to_env_only_for_main(store: CredentialStore, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(DEFAULT_ENV_KEY, "from-env")
    resolver = TokenResolver(store)
    assert resolver.resolve("main") == "from-env"
    # non-`main` accounts must not silently inherit the env token
    assert resolver.resolve("secondary") is None


def test_resolve_prefers_store_over_env(store: CredentialStore, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store.write("telegram", "main", {"token": "stored"})
    monkeypatch.setenv(DEFAULT_ENV_KEY, "from-env")
    assert TokenResolver(store).resolve("main") == "stored"


def test_require_raises_user_visible_when_missing(store: CredentialStore) -> None:
    with pytest.raises(UserVisibleError):
        TokenResolver(store).require("main")


def test_write_rejects_empty_token(store: CredentialStore) -> None:
    with pytest.raises(ValueError):
        TokenResolver(store).write("main", "")


def test_rotate_returns_previous(store: CredentialStore) -> None:
    resolver = TokenResolver(store)
    resolver.write("main", "old")
    assert resolver.rotate("main", "new") == "old"
    assert resolver.resolve("main") == "new"


def test_rotate_returns_none_if_no_previous(store: CredentialStore) -> None:
    assert TokenResolver(store).rotate("fresh", "new") is None


def test_forget_round_trips(store: CredentialStore) -> None:
    resolver = TokenResolver(store)
    resolver.write("main", "abc")
    assert resolver.forget("main") is True
    assert resolver.forget("main") is False
    assert resolver.resolve("main") is None
