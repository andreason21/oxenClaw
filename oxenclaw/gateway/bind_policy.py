"""Bind-host policy: forbid remote exposure of the gateway by default.

The oxenClaw security model assumes "the agent runs as the local OS
user and is reachable only by that user on this machine." A loopback
bind makes that the case automatically: only processes on the same
host can connect, and the bearer-token check filters those further.

The moment the gateway binds to a non-loopback address (`0.0.0.0`,
`::`, a LAN IP, or a hostname that resolves to a non-loopback IP),
the principal expands to "anyone on the same network with the
token." That can be a legitimate setup — reverse proxy in front, k8s
Service, internal corporate net — but it should be an explicit,
loud choice instead of an accidental default.

This module enforces that:

- Loopback hosts (`127.0.0.1`, `::1`, `localhost`) are always allowed.
- Non-loopback binds raise `RemoteBindRefused` unless the operator
  passes `--allow-non-loopback` on the CLI or sets
  `OXENCLAW_ALLOW_NON_LOOPBACK=1` in the environment.
- When opted in, a loud `WARNING` is logged so operators see in their
  startup banner that the gateway is reachable beyond loopback.
"""

from __future__ import annotations

import ipaddress
import os

from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("gateway.bind_policy")

ENV_OPT_IN = "OXENCLAW_ALLOW_NON_LOOPBACK"

_LOOPBACK_NAMES: frozenset[str] = frozenset({"localhost", "ip6-localhost", "ip6-loopback"})


class RemoteBindRefused(ValueError):
    """Raised when the operator tries to bind beyond loopback without opting in."""


def is_loopback_host(host: str) -> bool:
    """Return True iff `host` is a loopback literal or one of the
    well-known loopback hostnames.

    Hostnames that aren't `localhost`/`ip6-localhost`/`ip6-loopback`
    are treated as non-loopback even if they happen to resolve to one
    today — DNS results change, the security stance shouldn't.
    """
    h = host.strip().lower()
    if h in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def is_unspecified_host(host: str) -> bool:
    """`0.0.0.0` / `::` / empty — bind on all interfaces."""
    h = host.strip()
    if not h:
        return True
    try:
        return ipaddress.ip_address(h).is_unspecified
    except ValueError:
        return False


def env_opt_in() -> bool:
    return os.environ.get(ENV_OPT_IN, "").strip().lower() in ("1", "true", "yes", "on")


def validate_bind_host(host: str, *, allow_non_loopback: bool = False) -> None:
    """Raise `RemoteBindRefused` if `host` would expose the gateway
    beyond loopback and the operator hasn't opted in.

    When the bind is permitted but non-loopback (opt-in path), a
    WARNING is logged so the startup banner shows the security
    stance has been widened on purpose.
    """
    if is_loopback_host(host):
        return
    if allow_non_loopback or env_opt_in():
        logger.warning(
            "gateway binding to %r — accessible beyond loopback. "
            "Token-based auth still applies, but the principal model "
            "now extends to anyone on this network with the token. "
            "Make sure --auth-token and --allowed-origins are set, "
            "and consider TLS/mTLS at a reverse proxy in front.",
            host,
        )
        return
    kind = "wildcard (all interfaces)" if is_unspecified_host(host) else "non-loopback"
    raise RemoteBindRefused(
        f"refusing to bind gateway to {kind} host {host!r}. "
        f"oxenClaw defaults to loopback so the agent runs only for the "
        f"local OS user on this machine. To bind beyond loopback (reverse "
        f"proxy, k8s Service, internal corp net), pass "
        f"--allow-non-loopback or set {ENV_OPT_IN}=1 — and verify that "
        f"--auth-token and --allowed-origins are configured appropriately."
    )


__all__ = [
    "ENV_OPT_IN",
    "RemoteBindRefused",
    "env_opt_in",
    "is_loopback_host",
    "is_unspecified_host",
    "validate_bind_host",
]
