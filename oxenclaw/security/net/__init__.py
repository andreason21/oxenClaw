"""Outbound + inbound network guards.

Layers (matches openclaw's architecture):

- `policy`         — `NetPolicy` (allow/deny hostname globs, ports, private
                     network flag) + `merge_policies()`.
- `ssrf`           — host classification (loopback, private, link-local,
                     CGNAT, IPv4-in-IPv6, legacy literals) + URL validation.
- `pinning`        — DNS-pinning aiohttp resolver: resolve once, validate
                     against NetPolicy, refuse rebinding.
- `audit`          — opt-in outbound audit log via `aiohttp.TraceConfig`
                     backed by a sqlite WAL store.
- `guarded_fetch`  — single entrypoint that combines policy + pinning +
                     audit so callers don't assemble it themselves.
- `webhook_guards` — body-size limiter, fixed-window rate limiter, HMAC
                     signature verification, pre-auth/post-auth profile.

Env knobs:
- `OXENCLAW_NET_ALLOW_HOSTS=*.example.com,api.openai.com`
- `OXENCLAW_NET_DENY_HOSTS=*.internal`
- `OXENCLAW_NET_ALLOW_PRIVATE=1`  (default 0)
- `OXENCLAW_AUDIT_OUTBOUND=1`     (default off)
- `OXENCLAW_AUDIT_OUTBOUND_BODY=1` (default off; expensive)
- `OXENCLAW_AUDIT_OUTBOUND_PATH=/path/to/audit.db`
"""

from oxenclaw.security.net.policy import (
    NetPolicy,
    NetPolicyError,
    hostname_matches,
    merge_policies,
    policy_from_env,
)

__all__ = [
    "NetPolicy",
    "NetPolicyError",
    "hostname_matches",
    "merge_policies",
    "policy_from_env",
]
