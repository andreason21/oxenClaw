"""MiniMax wrapper.

Mirrors `minimax-stream-wrappers.ts`. OpenAI-shape; MiniMax's tool calling
expects `tool_choice="auto"` and uses `MM-API-SOURCE` header for trace.
"""

from __future__ import annotations

from typing import Any

from oxenclaw.pi.providers._openai_shared import stream_openai_compatible
from oxenclaw.pi.streaming import register_provider_stream


def _minimax_payload_patch(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("tools") and "tool_choice" not in payload:
        payload["tool_choice"] = "auto"
    return payload


async def stream_minimax(ctx, opts):  # type: ignore[no-untyped-def]
    async for ev in stream_openai_compatible(ctx, opts, payload_patch=_minimax_payload_patch):
        yield ev


register_provider_stream("minimax", stream_minimax)
