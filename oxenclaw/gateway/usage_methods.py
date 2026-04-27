"""`usage.*` RPCs — per-session and aggregated token / cost telemetry.

PiAgent writes a JSON file alongside each `ConversationHistory` capturing
cumulative `(input, output, cache_read, cache_create, cost_usd, turns,
hit_rate)` for that session. These methods read and aggregate those
files; nothing is computed at request time so the RPC is cheap.

Design notes:
- Cost only appears when the active model has a `pricing` dict on its
  registry entry (USD per million tokens, keyed by token category).
  Models without pricing report `cost_usd: 0.0`.
- The session-key indirection (channel:account:chat_id[:thread]) is
  identical to `chat.history`'s, so dashboards can call both with the
  same key.
- `usage.totals` walks the agent's session dir, so it scales with the
  number of session files. Tens of thousands is fine; if anyone hits a
  larger fleet, switch to a small SQLite roll-up.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from oxenclaw.config.paths import OxenclawPaths, default_paths
from oxenclaw.gateway.router import Router


class _SessionParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str
    session_key: str


class _TotalsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str | None = None


def _empty() -> dict[str, Any]:
    return {
        "turns": 0,
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_create": 0,
        "cost_usd": 0.0,
        "hit_rate": 0.0,
    }


def _load_one(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty()
    base = _empty()
    if isinstance(data, dict):
        for k in base:
            if k in data and isinstance(data[k], (int, float)):
                base[k] = data[k]
    return base


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out = _empty()
    if not rows:
        return out
    total_read = 0
    total_input = 0
    cost = 0.0
    for r in rows:
        out["turns"] += int(r.get("turns", 0))
        out["input"] += int(r.get("input", 0))
        out["output"] += int(r.get("output", 0))
        out["cache_read"] += int(r.get("cache_read", 0))
        out["cache_create"] += int(r.get("cache_create", 0))
        cost += float(r.get("cost_usd", 0.0))
        total_read += int(r.get("cache_read", 0))
        total_input += int(r.get("input", 0))
    out["cost_usd"] = round(cost, 6)
    denom = total_read + total_input
    out["hit_rate"] = round(total_read / denom, 4) if denom > 0 else 0.0
    return out


class _AccountParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


def register_usage_methods(
    router: Router,
    *,
    paths: OxenclawPaths | None = None,
    auth_pool: Any = None,
) -> None:
    resolved = paths or default_paths()

    @router.method("usage.session", _SessionParams)
    async def _usage_session(p: _SessionParams) -> dict[str, Any]:  # type: ignore[type-arg]
        return _load_one(resolved.usage_file(p.agent_id, p.session_key))

    @router.method("usage.totals", _TotalsParams)
    async def _usage_totals(p: _TotalsParams) -> dict[str, Any]:  # type: ignore[type-arg]
        if p.agent_id:
            agent_dirs = [resolved.agent_dir(p.agent_id)]
        else:
            base = resolved.agents_dir
            agent_dirs = sorted(base.iterdir()) if base.exists() else []

        per_agent: list[dict[str, Any]] = []
        all_rows: list[dict[str, Any]] = []
        for adir in agent_dirs:
            sessions_dir = adir / "sessions"
            if not sessions_dir.exists():
                continue
            rows = []
            for f in sorted(sessions_dir.glob("*.usage.json")):
                rows.append(_load_one(f))
            agg = _aggregate(rows)
            per_agent.append({"agent_id": adir.name, **agg})
            all_rows.extend(rows)
        return {
            "total": _aggregate(all_rows),
            "per_agent": per_agent,
        }

    @router.method("usage.account", _AccountParams)
    async def _usage_account(_: _AccountParams) -> dict[str, Any]:  # type: ignore[type-arg]
        """Return per-provider account-usage snapshots for every
        provider configured in the auth pool. Snapshots that come back
        as None (no OAuth token, 404, etc.) are dropped from the list."""
        from oxenclaw.pi.account_usage import fetch_account_usage

        snapshots: list[dict[str, Any]] = []
        if auth_pool is None:
            return {"snapshots": snapshots}
        providers = list(getattr(auth_pool, "by_provider", {}).keys())
        for provider in providers:
            try:
                creds = await auth_pool.get(provider)
            except Exception:
                continue
            if creds is None:
                continue
            _, api_key = creds
            try:
                snap = await fetch_account_usage(provider, api_key)
            except Exception:
                snap = None
            if snap is not None:
                snapshots.append(snap.to_dict())
        return {"snapshots": snapshots}
