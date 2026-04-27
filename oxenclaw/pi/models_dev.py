"""models.dev integration — remote model catalog with offline fallback.

A thin port of `hermes-agent/agent/models_dev.py`. Resolves model
metadata from https://models.dev/api.json with a four-step cascade:

1. In-memory TTL cache (1 hour by default).
2. Disk cache at `~/.oxenclaw/cache/models_dev.json` with a stored
   `_fetched_at` timestamp; honoured if fresh, used as stale fallback
   when network fetch fails.
3. Bundled snapshot at `oxenclaw/data/models_dev_snapshot.json`
   shipping a curated subset (Claude 4.7/4.6/Haiku, GPT-5/4o/o3,
   Gemini 2.5/2.0, DeepSeek, Qwen3) so a fresh install can resolve
   common models even with no network and an empty cache dir.
4. Network fetch via `urllib.request` (5s timeout, no extra deps).

`get_model_capabilities(model_id, data)` walks every provider in `data`
to find the model and returns a normalised capability dict. The lookup
is provider-agnostic — callers don't need to know which provider key
the id lives under.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MODELS_DEV_URL = "https://models.dev/api.json"
CACHE_TTL_SECONDS = 3600
DISK_CACHE_PATH = Path.home() / ".oxenclaw" / "cache" / "models_dev.json"

# Probe tiers used when models.dev has no entry — the runner picks the
# largest credible context window so it doesn't waste headroom, then
# downgrades on actual provider errors. Mirrors hermes' tiered probe.
CONTEXT_PROBE_TIERS: tuple[int, ...] = (
    256_000,
    128_000,
    64_000,
    32_000,
    16_000,
    8_000,
)

# Bundled snapshot shipped with the package (offline-first fallback).
_SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "data" / "models_dev_snapshot.json"

# In-memory cache.
_cache_lock = threading.Lock()
_cached_data: dict[str, Any] | None = None
_cached_at: float = 0.0


def _now() -> float:
    return time.time()


def _read_disk_cache() -> tuple[dict[str, Any] | None, float]:
    """Return (data, fetched_at) from disk; (None, 0) on failure."""
    try:
        if not DISK_CACHE_PATH.exists():
            return None, 0.0
        text = DISK_CACHE_PATH.read_text(encoding="utf-8")
        wrapper = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("models_dev disk cache unreadable: %s", exc)
        return None, 0.0
    if not isinstance(wrapper, dict):
        return None, 0.0
    data = wrapper.get("data")
    fetched_at = wrapper.get("_fetched_at", 0.0)
    if not isinstance(data, dict):
        return None, 0.0
    try:
        fetched_at_f = float(fetched_at)
    except (TypeError, ValueError):
        fetched_at_f = 0.0
    return data, fetched_at_f


def _write_disk_cache(data: dict[str, Any]) -> None:
    try:
        DISK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        wrapper = {"_fetched_at": _now(), "data": data}
        tmp = DISK_CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(wrapper), encoding="utf-8")
        tmp.replace(DISK_CACHE_PATH)
    except OSError as exc:
        logger.debug("models_dev disk cache write failed: %s", exc)


def _load_snapshot() -> dict[str, Any]:
    """Load the bundled snapshot — never raises, returns {} on failure."""
    try:
        text = _SNAPSHOT_PATH.read_text(encoding="utf-8")
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("models_dev snapshot unreadable: %s", exc)
    return {}


def _fetch_network(timeout: float = 5.0) -> dict[str, Any] | None:
    """Fetch models.dev/api.json. Returns None on any error."""
    try:
        req = urllib.request.Request(
            MODELS_DEV_URL,
            headers={"User-Agent": "oxenclaw/models_dev"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
        data = json.loads(payload)
        if isinstance(data, dict):
            return data
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        logger.debug("models_dev network fetch failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("models_dev network fetch raised: %s", exc)
    return None


def fetch_models_dev(*, force_refresh: bool = False) -> dict[str, Any]:
    """Resolve the models.dev catalog using the four-step cascade.

    Order: in-memory TTL → fresh disk cache → network → stale disk
    cache → bundled snapshot. `force_refresh=True` skips the in-memory
    layer (still honours disk staleness logic).
    """
    global _cached_data, _cached_at
    with _cache_lock:
        if (
            not force_refresh
            and _cached_data is not None
            and (_now() - _cached_at) < CACHE_TTL_SECONDS
        ):
            return _cached_data

        # Try fresh disk cache first.
        if not force_refresh:
            disk_data, disk_at = _read_disk_cache()
            if disk_data and (_now() - disk_at) < CACHE_TTL_SECONDS:
                _cached_data = disk_data
                _cached_at = disk_at
                return disk_data

        # Network fetch.
        net = _fetch_network()
        if net is not None:
            _cached_data = net
            _cached_at = _now()
            _write_disk_cache(net)
            return net

        # Network down → stale disk cache.
        disk_data, disk_at = _read_disk_cache()
        if disk_data:
            _cached_data = disk_data
            _cached_at = disk_at
            return disk_data

        # Last resort: bundled snapshot. Don't store its timestamp, so a
        # later call still tries the network.
        snapshot = _load_snapshot()
        _cached_data = snapshot
        _cached_at = _now() - CACHE_TTL_SECONDS + 60  # short retry
        return snapshot


def _provider_for_model(model_id: str, data: dict[str, Any]) -> str | None:
    """Locate which provider key holds `model_id`."""
    for provider_id, provider_data in data.items():
        if not isinstance(provider_data, dict):
            continue
        models = provider_data.get("models")
        if not isinstance(models, dict):
            continue
        if model_id in models:
            return provider_id
        # Case-insensitive fallback.
        ml = model_id.lower()
        for mid in models:
            if isinstance(mid, str) and mid.lower() == ml:
                return provider_id
    return None


def _get_model_entry(model_id: str, data: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    provider_id = _provider_for_model(model_id, data)
    if provider_id is None:
        return None, None
    provider_data = data.get(provider_id)
    if not isinstance(provider_data, dict):
        return provider_id, None
    models = provider_data.get("models")
    if not isinstance(models, dict):
        return provider_id, None
    entry = models.get(model_id)
    if not isinstance(entry, dict):
        ml = model_id.lower()
        for mid, val in models.items():
            if isinstance(mid, str) and mid.lower() == ml and isinstance(val, dict):
                entry = val
                break
    return provider_id, entry if isinstance(entry, dict) else None


def get_model_capabilities(model_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Return a normalised capability dict for `model_id`.

    Keys:
      context_window: int | None — None when models.dev didn't list it.
      max_output:     int | None
      supports_tools: bool       — defaults True (most modern models).
      supports_attachments: bool
      supports_reasoning:   bool
      family:               str | None
      provider:             str | None — models.dev provider id.
    """
    provider_id, entry = _get_model_entry(model_id, data)
    if entry is None:
        return {
            "context_window": None,
            "max_output": None,
            "supports_tools": True,
            "supports_attachments": False,
            "supports_reasoning": False,
            "family": None,
            "provider": provider_id,
        }

    limit = entry.get("limit")
    if not isinstance(limit, dict):
        limit = {}
    ctx = limit.get("context")
    out = limit.get("output")
    context_window = int(ctx) if isinstance(ctx, (int, float)) and ctx > 0 else None
    max_output = int(out) if isinstance(out, (int, float)) and out > 0 else None

    # `attachment` is the canonical flag; some entries only list `image`
    # in `modalities.input`. Treat either as attachment support.
    attachment = bool(entry.get("attachment", False))
    if not attachment:
        modalities = entry.get("modalities")
        if isinstance(modalities, dict):
            input_mods = modalities.get("input")
            if isinstance(input_mods, list) and "image" in input_mods:
                attachment = True

    supports_tools = bool(entry.get("tool_call", True))
    supports_reasoning = bool(entry.get("reasoning", False))
    family = entry.get("family")
    if not isinstance(family, str) or not family:
        family = None

    return {
        "context_window": context_window,
        "max_output": max_output,
        "supports_tools": supports_tools,
        "supports_attachments": attachment,
        "supports_reasoning": supports_reasoning,
        "family": family,
        "provider": provider_id,
    }


def lookup_models_dev_context(model_id: str) -> int | None:
    """Convenience wrapper: fetch + capability lookup for context_window."""
    data = fetch_models_dev()
    caps = get_model_capabilities(model_id, data)
    ctx = caps.get("context_window")
    return ctx if isinstance(ctx, int) else None


def reset_cache_for_tests() -> None:
    """Clear in-memory cache. Tests use this to force a fresh fetch."""
    global _cached_data, _cached_at
    with _cache_lock:
        _cached_data = None
        _cached_at = 0.0


def models_dev_enabled() -> bool:
    """Operator opt-in to RemoteModelRegistry via env flag."""
    raw = os.environ.get("OXENCLAW_USE_MODELS_DEV", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


__all__ = [
    "CACHE_TTL_SECONDS",
    "CONTEXT_PROBE_TIERS",
    "DISK_CACHE_PATH",
    "MODELS_DEV_URL",
    "fetch_models_dev",
    "get_model_capabilities",
    "lookup_models_dev_context",
    "models_dev_enabled",
    "reset_cache_for_tests",
]
