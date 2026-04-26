"""Zhipu AI (智谱) GLM wrapper.

Mirrors `zai-stream-wrappers.ts`. OpenAI-shape with one quirk: GLM-4 plus
models accept a `do_sample` boolean, and tool calls require explicit
`tool_choice="auto"` to be returned in streaming form.
"""

from __future__ import annotations

from typing import Any

from oxenclaw.pi.providers._openai_shared import stream_openai_compatible
from oxenclaw.pi.streaming import register_provider_stream


def _zai_payload_patch(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("tools") and "tool_choice" not in payload:
        payload["tool_choice"] = "auto"
    payload.setdefault("do_sample", payload.get("temperature", 0.0) > 0)
    return payload


async def stream_zai(ctx, opts):  # type: ignore[no-untyped-def]
    async for ev in stream_openai_compatible(ctx, opts, payload_patch=_zai_payload_patch):
        yield ev


register_provider_stream("zai", stream_zai)
