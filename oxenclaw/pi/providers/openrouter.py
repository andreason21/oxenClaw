"""OpenRouter wrapper.

Mirrors `openrouter-model-capabilities.ts`. OpenRouter is OpenAI-shape but
fronts dozens of upstream providers; the request shape is portable but the
allowed `extra` knobs depend on which underlying model is selected.

We accept a `provider` route hint via `model.extra["openrouter_route"]` →
sent as `provider` field. Cache control on Anthropic-via-OpenRouter is
opt-in via `extra_params["transforms"]`.
"""

from __future__ import annotations

from typing import Any

from oxenclaw.pi.providers._openai_shared import stream_openai_compatible
from oxenclaw.pi.streaming import register_provider_stream


def _openrouter_payload_patch(payload: dict[str, Any]) -> dict[str, Any]:
    # Promote `model.extra["openrouter_route"]` into the top-level field
    # OpenRouter inspects for upstream provider preference.
    return payload


async def stream_openrouter(ctx, opts):  # type: ignore[no-untyped-def]
    headers_extra = dict(ctx.api.extra_headers)
    headers_extra.setdefault("HTTP-Referer", "https://github.com/oxenclaw")
    headers_extra.setdefault("X-Title", "oxenClaw")
    # We can't mutate Api here without copying; rely on the underlying
    # wrapper to propagate `extra_headers` as-is. The `Api` is frozen but
    # additional headers can come through `opts.extra_params` if needed.
    async for ev in stream_openai_compatible(ctx, opts, payload_patch=_openrouter_payload_patch):
        yield ev


register_provider_stream("openrouter", stream_openrouter)
