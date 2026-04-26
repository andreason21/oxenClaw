"""guarded_session — single entrypoint for outbound HTTP.

Composes:
- `NetPolicy` (host/port/scheme allowlist + private-network gate)
- `PinnedResolver` via `make_guarded_connector` (DNS-rebinding defense)
- `make_audit_trace_config` (opt-in via env)
- per-request URL pre-flight via `assert_url_allowed`

Callers should prefer this over building their own `aiohttp.ClientSession`
so policy + audit are applied consistently across web tools, provider
streams, plugins.

Usage:

    from oxenclaw.security.net import policy_from_env
    from oxenclaw.security.net.guarded_fetch import guarded_session

    policy = policy_from_env()
    async with guarded_session(policy) as session:
        async with session.get("https://api.example.com/...") as resp:
            ...
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import aiohttp

from oxenclaw.security.net.audit import (
    AuditConfig,
    OutboundAuditStore,
    make_audit_trace_config,
    should_audit_from_env,
)
from oxenclaw.security.net.pinning import (
    make_guarded_connector,
)
from oxenclaw.security.net.policy import NetPolicy
from oxenclaw.security.net.ssrf import assert_url_allowed

# Module-level singleton audit store so repeated `guarded_session` calls
# share one DB instead of opening N connections.
_AUDIT_STORE: OutboundAuditStore | None = None


def _get_audit_store(cfg: AuditConfig) -> OutboundAuditStore | None:
    if not cfg.enabled or cfg.db_path is None:
        return None
    global _AUDIT_STORE
    if _AUDIT_STORE is None or _AUDIT_STORE.path != cfg.db_path:
        if _AUDIT_STORE is not None:
            _AUDIT_STORE.close()
        _AUDIT_STORE = OutboundAuditStore(cfg.db_path, max_body_bytes=cfg.max_body_bytes)
    return _AUDIT_STORE


def _close_audit_store() -> None:
    """Close the module-level audit store. Tests call this to reset state
    between runs."""
    global _AUDIT_STORE
    if _AUDIT_STORE is not None:
        _AUDIT_STORE.close()
        _AUDIT_STORE = None


@asynccontextmanager
async def guarded_session(
    policy: NetPolicy,
    *,
    audit: AuditConfig | None = None,
    timeout_total: float = 30.0,
    extra_headers: dict[str, str] | None = None,
):
    """Yield an `aiohttp.ClientSession` configured with `policy` + audit.

    `audit` defaults to env-derived config (`should_audit_from_env`) so
    operators can flip it on without code changes.
    """
    audit_cfg = audit if audit is not None else should_audit_from_env()
    connector = make_guarded_connector(policy)
    trace_configs = []
    store = _get_audit_store(audit_cfg)
    if store is not None:
        trace_configs.append(
            make_audit_trace_config(
                store,
                sample_rate=audit_cfg.sample_rate,
                capture_body=audit_cfg.capture_body,
            )
        )
    timeout = aiohttp.ClientTimeout(total=timeout_total)
    session = aiohttp.ClientSession(
        connector=connector,
        trace_configs=trace_configs or None,
        timeout=timeout,
        headers=extra_headers,
    )
    try:
        yield session
    finally:
        await session.close()


def policy_pre_flight(url: str, policy: NetPolicy) -> str:
    """Validate a URL against `policy` before issuing the request.

    Combines scheme/port/hostname checks. Use this *and* `guarded_session`
    — the session enforces at the resolver level (defends rebinding) and
    this catches obvious policy violations before opening a socket.
    """
    return assert_url_allowed(url, policy)


__all__ = [
    "_close_audit_store",
    "guarded_session",
    "policy_pre_flight",
]
