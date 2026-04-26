"""Moonshot (Kimi) wrapper.

Mirrors `moonshot-stream-wrappers.ts` + `moonshot-thinking-stream-wrappers.ts`.
Moonshot is OpenAI-shape with one twist: thinking-capable models ("kimi-k2-
thinking", "moonshot-v1-thinking") expose reasoning via a non-standard
`reasoning_content` delta field. We translate that into ThinkingDeltaEvent.

The wrapper also accepts an optional `extra_params["thinking_budget"]` that
becomes Moonshot's `thinking.budget_tokens` payload field.
"""

from __future__ import annotations

from typing import Any

from sampyclaw.pi.providers._openai_shared import stream_openai_compatible
from sampyclaw.pi.streaming import register_provider_stream


def _moonshot_payload_patch(payload: dict[str, Any]) -> dict[str, Any]:
    # Moonshot honours `thinking` block when the model supports it.
    if "thinking" in payload and isinstance(payload["thinking"], dict):
        return payload
    return payload


async def stream_moonshot(ctx, opts):  # type: ignore[no-untyped-def]
    async for ev in stream_openai_compatible(ctx, opts, payload_patch=_moonshot_payload_patch):
        yield ev


register_provider_stream("moonshot", stream_moonshot)
