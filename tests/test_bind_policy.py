"""Tests for `gateway/bind_policy.py` — refuses non-loopback binds
unless explicitly opted in.

The principal model: by default the agent is reachable only by the
local OS user on the same machine. Loosening that needs an explicit
flag so it's never accidental.
"""

from __future__ import annotations

import pytest

from sampyclaw.gateway.bind_policy import (
    ENV_OPT_IN,
    RemoteBindRefused,
    is_loopback_host,
    is_unspecified_host,
    validate_bind_host,
)

# ─── classifiers ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.0.0.2",
        "127.255.255.255",
        "::1",
        "localhost",
        "Localhost",
        "  localhost  ",
        "ip6-localhost",
        "ip6-loopback",
    ],
)
def test_loopback_hosts_are_classified(host: str) -> None:
    assert is_loopback_host(host)


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",
        "::",
        "192.168.1.5",
        "10.0.0.1",
        "203.0.113.7",
        "internal-vllm.lan",
        "example.com",
        "",
    ],
)
def test_non_loopback_hosts_are_not_classified_as_loopback(host: str) -> None:
    assert not is_loopback_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", ""])
def test_unspecified_hosts(host: str) -> None:
    assert is_unspecified_host(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "192.168.1.5"])
def test_specified_hosts_are_not_unspecified(host: str) -> None:
    assert not is_unspecified_host(host)


# ─── validate_bind_host ───────────────────────────────────────────────


def test_loopback_always_passes_without_opt_in() -> None:
    # No env, no flag — should still succeed for the loopback default.
    validate_bind_host("127.0.0.1")
    validate_bind_host("::1")
    validate_bind_host("localhost")


def test_wildcard_bind_refused_without_opt_in(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv(ENV_OPT_IN, raising=False)
    with pytest.raises(RemoteBindRefused) as exc_info:
        validate_bind_host("0.0.0.0")
    msg = str(exc_info.value)
    assert "non-loopback" in msg or "wildcard" in msg
    assert "--allow-non-loopback" in msg
    assert ENV_OPT_IN in msg


def test_lan_ip_refused_without_opt_in(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv(ENV_OPT_IN, raising=False)
    with pytest.raises(RemoteBindRefused):
        validate_bind_host("192.168.1.10")


def test_hostname_refused_without_opt_in_even_if_resolves_to_loopback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A custom hostname is treated as non-loopback even if today it
    resolves locally — DNS is mutable, the security stance shouldn't
    depend on resolution results."""
    monkeypatch.delenv(ENV_OPT_IN, raising=False)
    with pytest.raises(RemoteBindRefused):
        validate_bind_host("my-machine.local")


def test_explicit_flag_allows_non_loopback(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    import logging

    caplog.set_level(logging.WARNING, logger="sampyclaw.gateway.bind_policy")
    monkeypatch.delenv(ENV_OPT_IN, raising=False)
    validate_bind_host("0.0.0.0", allow_non_loopback=True)
    assert any("beyond loopback" in r.message for r in caplog.records)


def test_env_opt_in_allows_non_loopback(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    import logging

    caplog.set_level(logging.WARNING, logger="sampyclaw.gateway.bind_policy")
    monkeypatch.setenv(ENV_OPT_IN, "1")
    validate_bind_host("192.168.1.10")
    assert any("beyond loopback" in r.message for r in caplog.records)


@pytest.mark.parametrize("env_value", ["", "0", "false", "no", "off"])
def test_env_falsey_values_do_not_opt_in(monkeypatch, env_value: str) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(ENV_OPT_IN, env_value)
    with pytest.raises(RemoteBindRefused):
        validate_bind_host("0.0.0.0")


@pytest.mark.parametrize("env_value", ["1", "true", "True", "YES", "on"])
def test_env_truthy_values_opt_in(monkeypatch, env_value: str) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv(ENV_OPT_IN, env_value)
    # Should NOT raise.
    validate_bind_host("0.0.0.0")
