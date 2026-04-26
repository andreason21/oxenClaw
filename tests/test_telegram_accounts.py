"""Tests for TelegramAccountRegistry: config-driven multi-account loading."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from oxenclaw.config.credentials import CredentialStore
from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.extensions.telegram.accounts import TelegramAccountRegistry
from oxenclaw.extensions.telegram.channel import TelegramChannel
from oxenclaw.extensions.telegram.token import TokenResolver
from oxenclaw.plugin_sdk.config_schema import (
    AccountConfig,
    ChannelConfig,
    RootConfig,
)
from oxenclaw.plugin_sdk.error_runtime import UserVisibleError


@pytest.fixture()
def store(tmp_path) -> CredentialStore:  # type: ignore[no-untyped-def]
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    return CredentialStore(paths)


@pytest.fixture()
def patch_create_bot(monkeypatch):  # type: ignore[no-untyped-def]
    def _fake(token: str) -> MagicMock:
        bot = MagicMock()
        bot.session = MagicMock()
        bot.session.close = AsyncMock()
        return bot

    monkeypatch.setattr("oxenclaw.extensions.telegram.channel.create_bot", _fake)
    return monkeypatch


def _config(*account_ids: str) -> RootConfig:
    return RootConfig(
        channels={
            "telegram": ChannelConfig(
                accounts=[AccountConfig(account_id=aid) for aid in account_ids]
            )
        }
    )


def test_load_from_config_registers_accounts_with_tokens(
    store: CredentialStore, patch_create_bot
) -> None:  # type: ignore[no-untyped-def]
    store.write("telegram", "main", {"token": "t-main"})
    store.write("telegram", "secondary", {"token": "t-secondary"})
    reg = TelegramAccountRegistry(tokens=TokenResolver(store))

    loaded = reg.load_from_config(_config("main", "secondary"))
    assert sorted(loaded) == ["main", "secondary"]
    assert reg.ids() == ["main", "secondary"]
    assert isinstance(reg.get("main"), TelegramChannel)


def test_load_skips_accounts_without_tokens(store: CredentialStore, patch_create_bot) -> None:  # type: ignore[no-untyped-def]
    store.write("telegram", "main", {"token": "t"})
    reg = TelegramAccountRegistry(tokens=TokenResolver(store))
    loaded = reg.load_from_config(_config("main", "missing"))
    assert loaded == ["main"]
    assert reg.missing(_config("main", "missing")) == ["missing"]


def test_load_is_idempotent(store: CredentialStore, patch_create_bot) -> None:  # type: ignore[no-untyped-def]
    store.write("telegram", "main", {"token": "t"})
    reg = TelegramAccountRegistry(tokens=TokenResolver(store))
    reg.load_from_config(_config("main"))
    first = reg.get("main")
    reg.load_from_config(_config("main"))
    assert reg.get("main") is first  # not replaced


def test_load_with_no_telegram_config_returns_empty(
    store: CredentialStore,
) -> None:  # type: ignore[no-untyped-def]
    reg = TelegramAccountRegistry(tokens=TokenResolver(store))
    assert reg.load_from_config(RootConfig()) == []


def test_require_raises_when_missing(
    store: CredentialStore,
) -> None:  # type: ignore[no-untyped-def]
    reg = TelegramAccountRegistry(tokens=TokenResolver(store))
    with pytest.raises(UserVisibleError):
        reg.require("nope")


def test_register_rejects_duplicates(store: CredentialStore, patch_create_bot) -> None:  # type: ignore[no-untyped-def]
    reg = TelegramAccountRegistry(tokens=TokenResolver(store))
    ch = TelegramChannel(token="t", account_id="main")
    reg.register(ch)
    with pytest.raises(ValueError):
        reg.register(TelegramChannel(token="t", account_id="main"))


async def test_aclose_closes_every_channel(store: CredentialStore, patch_create_bot) -> None:  # type: ignore[no-untyped-def]
    store.write("telegram", "main", {"token": "t"})
    reg = TelegramAccountRegistry(tokens=TokenResolver(store))
    reg.load_from_config(_config("main"))
    await reg.aclose()
    assert reg.ids() == []
