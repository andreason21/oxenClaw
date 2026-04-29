"""Wire-level LLM request/response trace.

Mirrors openclaw's `OPENCLAW_CACHE_TRACE` (src/agents/cache-trace.ts) but
focuses on what oxenclaw was missing: the *final* JSON payload sent to
the provider's HTTP endpoint and the assembled response (content +
tool_calls + finish_reason + usage). That's the byte-for-byte evidence
needed to debug "why didn't the model call the tool?" — system-prompt
debug RPCs only show the prompt the agent *intended* to send, not what
went over the wire after every payload patch.

Activation:

    OXENCLAW_LLM_TRACE=1                      # enable
    OXENCLAW_LLM_TRACE_FILE=/tmp/llm.jsonl    # override sink (optional)
    OXENCLAW_LLM_TRACE_MAX_BODY=200000        # truncate huge fields (optional)

Default sink: `~/.oxenclaw/logs/llm-trace.jsonl` (sibling of gateway.log).
Each line is a self-contained JSON event:

    {"ts":"...", "event":"request",  "provider":"...", "model":"...",
     "url":"...", "payload":{...}}
    {"ts":"...", "event":"response", "provider":"...", "model":"...",
     "content":"...", "tool_calls":[...], "finish_reason":"...",
     "usage":{...}, "duration_ms":1234}
    {"ts":"...", "event":"error",    "provider":"...", "model":"...",
     "status":429, "message":"..."}

`request` and `response` share a `request_id` field so you can `jq`
filter a single round-trip.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("observability.llm_trace")

_DEFAULT_MAX_BODY = 200_000  # chars — guard against multi-MB image base64


def is_enabled() -> bool:
    return os.environ.get("OXENCLAW_LLM_TRACE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _resolve_sink() -> Path:
    override = os.environ.get("OXENCLAW_LLM_TRACE_FILE", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    home = os.environ.get("OXENCLAW_HOME", "").strip()
    base = Path(os.path.expanduser(home)) if home else Path.home() / ".oxenclaw"
    return base / "logs" / "llm-trace.jsonl"


def _max_body() -> int:
    raw = os.environ.get("OXENCLAW_LLM_TRACE_MAX_BODY", "").strip()
    if not raw:
        return _DEFAULT_MAX_BODY
    try:
        return max(1024, int(raw))
    except ValueError:
        return _DEFAULT_MAX_BODY


def _truncate(value: Any, *, limit: int) -> Any:
    """Best-effort truncation so a giant payload (image base64) doesn't
    explode the trace file. We only trim string leaves; structure stays
    intact so a tail -f stays grep-friendly."""
    if isinstance(value, str):
        if len(value) > limit:
            return value[:limit] + f"...[truncated {len(value) - limit} chars]"
        return value
    if isinstance(value, list):
        return [_truncate(v, limit=limit) for v in value]
    if isinstance(value, dict):
        return {k: _truncate(v, limit=limit) for k, v in value.items()}
    return value


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def _write(record: dict[str, Any]) -> None:
    sink = _resolve_sink()
    try:
        sink.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with sink.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        # Tracing must never break a real request — log once at debug
        # and move on.
        logger.debug("llm_trace write failed: %s", exc)


def log_request(
    *,
    request_id: str,
    provider: str,
    model_id: str,
    url: str,
    payload: dict[str, Any],
) -> None:
    if not is_enabled():
        return
    limit = _max_body()
    record = {
        "ts": _iso_now(),
        "event": "request",
        "request_id": request_id,
        "provider": provider,
        "model": model_id,
        "url": url,
        "payload": _truncate(payload, limit=limit),
    }
    _write(record)


def log_response(
    *,
    request_id: str,
    provider: str,
    model_id: str,
    content: str,
    tool_calls: list[dict[str, Any]],
    finish_reason: str | None,
    usage: dict[str, Any] | None,
    duration_ms: float,
) -> None:
    if not is_enabled():
        return
    limit = _max_body()
    record = {
        "ts": _iso_now(),
        "event": "response",
        "request_id": request_id,
        "provider": provider,
        "model": model_id,
        "content": _truncate(content, limit=limit),
        "tool_calls": _truncate(tool_calls, limit=limit),
        "finish_reason": finish_reason,
        "usage": usage,
        "duration_ms": round(duration_ms, 2),
    }
    _write(record)


def log_error(
    *,
    request_id: str,
    provider: str,
    model_id: str,
    status: int | None,
    message: str,
    duration_ms: float,
) -> None:
    if not is_enabled():
        return
    record = {
        "ts": _iso_now(),
        "event": "error",
        "request_id": request_id,
        "provider": provider,
        "model": model_id,
        "status": status,
        "message": message[:2000],
        "duration_ms": round(duration_ms, 2),
    }
    _write(record)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


__all__ = [
    "is_enabled",
    "log_error",
    "log_request",
    "log_response",
    "new_request_id",
]
