"""`llamacpp-direct` provider: oxenclaw spawns and owns `llama-server`.

Why this exists alongside the existing `llamacpp` provider id:

- `llamacpp` (registered in `openai.py`) is the "external" mode —
  the user runs `llama-server` themselves and oxenclaw just talks to
  the OpenAI-compatible endpoint at the configured `base_url`.
- `llamacpp-direct` (this module) is the "managed" mode — oxenclaw
  spawns `llama-server` itself with unsloth-studio's fast-preset flags
  (`--flash-attn on --jinja --no-context-shift -ngl 999`), waits for
  `/health`, then streams against `http://127.0.0.1:<picked_port>/v1`.

The same SSE wrapper from `_openai_shared` is reused; this module's
job is the lifecycle wiring + `ctx.api.base_url` rewrite.

Required configuration (one of):

- `model.extra["gguf_path"] = "/abs/path/to/model.gguf"` — preferred,
  carried in the `Model` registration.
- `OXENCLAW_LLAMACPP_GGUF=/abs/path/to/model.gguf` — process-wide
  fallback, useful when you only run one model at a time.

Optional knobs (env vars; `model.extra` keys override env):

- `OXENCLAW_LLAMACPP_NGL` (default 999, all layers on GPU)
- `OXENCLAW_LLAMACPP_CTX` (default 16384)
- `OXENCLAW_LLAMACPP_THREADS` (default -1, llama.cpp picks)
- `OXENCLAW_LLAMACPP_PARALLEL` (default 1)
- `OXENCLAW_LLAMACPP_EXTRA_ARGS` (extra flags, shell-split)
- `OXENCLAW_LLAMACPP_HEALTH_TIMEOUT_S` (default 600)
"""

from __future__ import annotations

import os
import shlex
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

from oxenclaw.pi.llamacpp_server import (
    LlamaCppServerError,
    get_default_server,
)
from oxenclaw.pi.llamacpp_server.manager import LlamaCppServerSpec
from oxenclaw.pi.models import Api, Context
from oxenclaw.pi.providers._openai_shared import stream_openai_compatible
from oxenclaw.pi.streaming import (
    AssistantMessageEvent,
    ErrorEvent,
    SimpleStreamOptions,
    StopEvent,
    register_provider_stream,
)


def _coerce_int(raw: str | None, default: int) -> int:
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _coerce_float(raw: str | None, default: float) -> float:
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _spec_from_context(ctx: Context) -> LlamaCppServerSpec:
    """Build a `LlamaCppServerSpec` from `ctx.model.extra` + env defaults.

    Resolution order: `model.extra[<key>]` > `OXENCLAW_LLAMACPP_<KEY>` env >
    hard-coded default. The user explicitly opted into the local-GGUF
    flow, so we treat a missing path as a hard error rather than try
    to discover one.
    """
    extra = dict(ctx.model.extra or {})

    raw_path = extra.get("gguf_path") or os.environ.get("OXENCLAW_LLAMACPP_GGUF", "").strip()
    if not raw_path:
        raise LlamaCppServerError(
            "llamacpp-direct: no GGUF path configured. Set "
            "model.extra['gguf_path'] in your model registration or export "
            "$OXENCLAW_LLAMACPP_GGUF=/abs/path/to/model.gguf."
        )
    gguf_path = Path(os.path.expanduser(str(raw_path)))

    # Default ctx 65536 — bundled assistant's system prompt (memory +
    # skill manifests + tool schemas) regularly clears 33K tokens on a
    # populated install. 32K was insufficient and produced repeated
    # `context_overflow` HTTP 400s; 64K leaves headroom for a turn or
    # two of conversation. Drop to 16384/32768 explicitly via
    # `OXENCLAW_LLAMACPP_CTX=...` only when VRAM is tight (~6 GiB
    # cards) and the system prompt has been trimmed accordingly.
    n_ctx = int(extra.get("n_ctx") or _coerce_int(os.environ.get("OXENCLAW_LLAMACPP_CTX"), 65536))
    n_gpu_layers = int(
        extra.get("n_gpu_layers")
        or _coerce_int(os.environ.get("OXENCLAW_LLAMACPP_NGL"), 999)
    )
    n_threads = int(
        extra.get("n_threads")
        or _coerce_int(os.environ.get("OXENCLAW_LLAMACPP_THREADS"), -1)
    )
    n_parallel = int(
        extra.get("n_parallel")
        or _coerce_int(os.environ.get("OXENCLAW_LLAMACPP_PARALLEL"), 1)
    )

    extra_args_raw = extra.get("extra_args")
    if isinstance(extra_args_raw, (list, tuple)):
        extra_args = tuple(str(a) for a in extra_args_raw)
    else:
        env_extra = os.environ.get("OXENCLAW_LLAMACPP_EXTRA_ARGS", "").strip()
        extra_args = tuple(shlex.split(env_extra)) if env_extra else ()

    chat_template = extra.get("chat_template") or os.environ.get("OXENCLAW_LLAMACPP_TEMPLATE")
    mmproj_raw = extra.get("mmproj_path") or os.environ.get("OXENCLAW_LLAMACPP_MMPROJ")
    mmproj_path = Path(os.path.expanduser(str(mmproj_raw))) if mmproj_raw else None

    return LlamaCppServerSpec(
        gguf_path=gguf_path,
        n_ctx=n_ctx,
        n_gpu_layers=n_gpu_layers,
        n_threads=n_threads,
        n_parallel=n_parallel,
        chat_template=str(chat_template) if chat_template else None,
        mmproj_path=mmproj_path,
        extra_args=extra_args,
    )


def _llamacpp_payload_patch(payload: dict) -> dict:
    """llama-server already accepts the OpenAI shape verbatim. The only
    field we strip is `stop` when empty (some llama-server builds reject
    an empty array)."""
    if "stop" in payload and not payload["stop"]:
        payload.pop("stop", None)
    return payload


async def _stream_llamacpp_direct(
    ctx: Context, opts: SimpleStreamOptions
) -> AsyncIterator[AssistantMessageEvent]:
    """Ensure the managed server is up, then stream via the OpenAI shim."""
    try:
        spec = _spec_from_context(ctx)
    except LlamaCppServerError as exc:
        yield ErrorEvent(message=str(exc), retryable=False, error=exc)
        yield StopEvent(reason="error")
        return

    server = get_default_server()
    health_timeout = _coerce_float(
        os.environ.get("OXENCLAW_LLAMACPP_HEALTH_TIMEOUT_S"), 600.0
    )
    try:
        base_url = server.ensure_loaded(spec, health_timeout_s=health_timeout)
    except LlamaCppServerError as exc:
        yield ErrorEvent(message=str(exc), retryable=False, error=exc)
        yield StopEvent(reason="error")
        return

    # Rewrite ctx so the shared OpenAI streamer hits the managed port.
    # `replace` keeps Context frozen-friendly and avoids leaking state
    # back to the caller's Model registration.
    new_api = Api(
        base_url=base_url,
        api_key=ctx.api.api_key,
        extra_headers=ctx.api.extra_headers,
    )
    patched_ctx = replace(ctx, api=new_api)

    async for ev in stream_openai_compatible(
        patched_ctx, opts, payload_patch=_llamacpp_payload_patch
    ):
        yield ev


register_provider_stream("llamacpp-direct", _stream_llamacpp_direct)


__all__ = ["_stream_llamacpp_direct"]
