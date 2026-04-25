"""Multi-registry config + MultiRegistryClient + token-source tests."""

from __future__ import annotations

import pytest

from sampyclaw.clawhub.registries import (
    ClawHubRegistries,
    MultiRegistryClient,
    RegistryConfig,
    builtin_public_registry,
    normalise,
)


def test_normalise_falls_back_to_public_when_empty() -> None:
    cfg = normalise(None)
    assert cfg.names() == ["public"]
    assert cfg.resolved_default() == "public"


def test_resolved_default_uses_first_when_unset() -> None:
    cfg = ClawHubRegistries(
        registries=[
            RegistryConfig(name="a", url="https://a"),
            RegistryConfig(name="b", url="https://b"),
        ],
    )
    assert cfg.resolved_default() == "a"


def test_resolve_token_inline_takes_priority() -> None:
    r = RegistryConfig(name="x", url="https://x", token="inline", token_env="UNUSED")
    assert r.resolve_token() == "inline"


def test_resolve_token_from_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MY_TOK", "from-env")
    r = RegistryConfig(name="x", url="https://x", token_env="MY_TOK")
    assert r.resolve_token() == "from-env"


def test_resolve_token_returns_none_when_neither_present() -> None:
    r = RegistryConfig(name="x", url="https://x")
    assert r.resolve_token() is None


def test_view_omits_token_value_but_flags_presence(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("T", "hidden")
    cfg = ClawHubRegistries(
        registries=[
            RegistryConfig(name="a", url="https://a", token_env="T"),
            RegistryConfig(name="b", url="https://b"),
        ]
    )
    multi = MultiRegistryClient(cfg)
    rows = multi.view()
    assert rows[0]["has_token"] is True
    assert "token" not in rows[0]
    assert rows[1]["has_token"] is False


def test_get_client_unknown_registry_raises() -> None:
    multi = MultiRegistryClient(ClawHubRegistries())
    with pytest.raises(KeyError):
        multi.get_client("ghost")


async def test_clients_cached_per_name() -> None:
    multi = MultiRegistryClient(
        ClawHubRegistries(
            registries=[
                RegistryConfig(name="a", url="https://a"),
                RegistryConfig(name="b", url="https://b"),
            ]
        )
    )
    a1 = multi.get_client("a")
    a2 = multi.get_client("a")
    b = multi.get_client("b")
    assert a1 is a2
    assert a1 is not b
    await multi.aclose()


def test_builtin_public_registry_is_official() -> None:
    r = builtin_public_registry()
    assert r.name == "public"
    assert r.trust == "official"
    assert r.url.startswith("https://")


def test_iter_clients_yields_in_config_order() -> None:
    multi = MultiRegistryClient(
        ClawHubRegistries(
            registries=[
                RegistryConfig(name="x", url="https://x"),
                RegistryConfig(name="y", url="https://y"),
            ]
        )
    )
    seen = [name for name, _ in multi.iter_clients()]
    assert seen == ["x", "y"]


def test_trust_lookup() -> None:
    multi = MultiRegistryClient(
        ClawHubRegistries(
            registries=[
                RegistryConfig(name="m", url="https://m", trust="mirror"),
                RegistryConfig(name="c", url="https://c", trust="community"),
            ]
        )
    )
    assert multi.trust("m") == "mirror"
    assert multi.trust("c") == "community"
    assert multi.trust("missing") == "community"  # default fallback
