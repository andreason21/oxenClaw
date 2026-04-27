"""Account usage normalisation API.

Thin port of `hermes-agent/agent/account_usage.py:127-326`. Surfaces the
"how much of my plan have I used?" API the dashboard needs without
pulling in the full hermes auth-token resolver — instead we accept an
`api_key` upfront and let callers pass keys from the auth pool.

The actual hermes implementation talks to `/api/oauth/usage` for
Anthropic and `/credits` + `/key` for OpenRouter. Most users won't
have the OAuth flow wired up; on a 404 / non-OAuth token we return
`None` so the dashboard hides the panel.

The `_make_request` seam allows tests to inject canned responses
without monkey-patching `httpx`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AccountUsageWindow:
    """One usage window — e.g. "Current session" or "Weekly quota"."""

    label: str
    used_percent: float
    reset_at: float | None = None
    detail: str = ""


@dataclass(frozen=True)
class AccountUsageSnapshot:
    """A normalised view of an account's quota across all windows."""

    provider: str
    windows: list[AccountUsageWindow] = field(default_factory=list)
    extra_credit: float | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "windows": [
                {
                    "label": w.label,
                    "used_percent": w.used_percent,
                    "reset_at": w.reset_at,
                    "detail": w.detail,
                }
                for w in self.windows
            ],
            "extra_credit": self.extra_credit,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# HTTP seam
# ---------------------------------------------------------------------------

# (method, url, headers, json_body) → response_dict | None.
RequestFn = Callable[[str, str, dict[str, str], dict[str, Any] | None], Awaitable[dict[str, Any] | None]]


async def _default_make_request(
    method: str,
    url: str,
    headers: dict[str, str],
    json_body: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Default httpx-based implementation. Returns None on any 4xx/5xx
    or transport error.

    Importing `httpx` lazily keeps the module test-friendly when httpx
    isn't installed — tests inject `_make_request` directly.
    """
    try:
        import httpx
    except ImportError:
        logger.debug("httpx not installed; account_usage requests disabled")
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if method.upper() == "POST":
                resp = await client.post(url, headers=headers, json=json_body)
            else:
                resp = await client.get(url, headers=headers)
            if resp.status_code in (404, 401, 403):
                return None
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001 — best-effort surface
        logger.debug("account_usage request to %s failed: %s", url, exc)
        return None


# Module-level seam tests can monkey-patch.
_make_request: RequestFn = _default_make_request


def set_request_fn(fn: RequestFn) -> None:
    """Test helper — replace the HTTP seam."""
    global _make_request
    _make_request = fn


def reset_request_fn() -> None:
    """Restore the default httpx-backed implementation."""
    global _make_request
    _make_request = _default_make_request


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------


def _to_percent(value: Any) -> float | None:
    """Coerce a 0..1 utilisation float (or 0..100 percent) to 0..100."""
    if not isinstance(value, (int, float)):
        return None
    f = float(value)
    if 0.0 <= f <= 1.0:
        return f * 100.0
    return f


def _parse_iso_to_epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.timestamp()


async def fetch_anthropic_usage(api_key: str) -> AccountUsageSnapshot | None:
    """Fetch Anthropic OAuth usage. Returns None for plain API keys
    (the OAuth usage endpoint 404s for those)."""
    if not api_key:
        return None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "oxenclaw/account-usage",
    }
    payload = await _make_request(
        "GET",
        "https://api.anthropic.com/api/oauth/usage",
        headers,
        None,
    )
    if payload is None:
        return None
    windows: list[AccountUsageWindow] = []
    mapping = (
        ("five_hour", "Current session"),
        ("seven_day", "Current week"),
        ("seven_day_opus", "Opus week"),
        ("seven_day_sonnet", "Sonnet week"),
    )
    for key, label in mapping:
        window = payload.get(key) or {}
        if not isinstance(window, dict):
            continue
        pct = _to_percent(window.get("utilization"))
        if pct is None:
            continue
        windows.append(
            AccountUsageWindow(
                label=label,
                used_percent=pct,
                reset_at=_parse_iso_to_epoch(window.get("resets_at")),
            )
        )

    extra_credit: float | None = None
    extra = payload.get("extra_usage") or {}
    if isinstance(extra, dict) and extra.get("is_enabled"):
        used = extra.get("used_credits")
        limit = extra.get("monthly_limit")
        if isinstance(used, (int, float)) and isinstance(limit, (int, float)):
            extra_credit = max(0.0, float(limit) - float(used))

    return AccountUsageSnapshot(
        provider="anthropic",
        windows=windows,
        extra_credit=extra_credit,
    )


async def fetch_openrouter_usage(api_key: str) -> AccountUsageSnapshot | None:
    """Fetch OpenRouter `/credits` + `/key` summary."""
    if not api_key:
        return None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    credits = await _make_request("GET", "https://openrouter.ai/api/v1/credits", headers, None)
    key = await _make_request("GET", "https://openrouter.ai/api/v1/key", headers, None)
    if credits is None and key is None:
        return None

    windows: list[AccountUsageWindow] = []
    extra_credit: float | None = None
    detail_parts: list[str] = []

    cdata = (credits or {}).get("data") or {}
    if isinstance(cdata, dict):
        total = cdata.get("total_credits")
        used = cdata.get("total_usage")
        if isinstance(total, (int, float)) and isinstance(used, (int, float)):
            extra_credit = max(0.0, float(total) - float(used))
            detail_parts.append(f"Credits balance: ${extra_credit:.2f}")

    kdata = (key or {}).get("data") or {}
    if isinstance(kdata, dict):
        limit = kdata.get("limit")
        remaining = kdata.get("limit_remaining")
        if (
            isinstance(limit, (int, float))
            and float(limit) > 0
            and isinstance(remaining, (int, float))
        ):
            limit_v = float(limit)
            remaining_v = float(remaining)
            used_pct = ((limit_v - remaining_v) / limit_v) * 100.0
            windows.append(
                AccountUsageWindow(
                    label="API key quota",
                    used_percent=used_pct,
                    detail=f"${remaining_v:.2f} of ${limit_v:.2f} remaining",
                )
            )

    return AccountUsageSnapshot(
        provider="openrouter",
        windows=windows,
        extra_credit=extra_credit,
        detail=" • ".join(detail_parts),
    )


async def fetch_account_usage(provider: str, api_key: str) -> AccountUsageSnapshot | None:
    """Dispatch to the per-provider fetcher. Unknown providers → None."""
    pid = (provider or "").strip().lower()
    if not pid or not api_key:
        return None
    if pid == "anthropic":
        return await fetch_anthropic_usage(api_key)
    if pid == "openrouter":
        return await fetch_openrouter_usage(api_key)
    return None


__all__ = [
    "AccountUsageSnapshot",
    "AccountUsageWindow",
    "fetch_account_usage",
    "fetch_anthropic_usage",
    "fetch_openrouter_usage",
    "reset_request_fn",
    "set_request_fn",
]
