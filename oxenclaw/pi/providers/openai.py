"""Local OpenAI-compatible providers (`vllm`, `lmstudio`, `llamacpp`).

oxenClaw's catalog is on-host-only by design (cloud / aggregator
providers were removed). The remaining OpenAI-shape providers all
target a local or LAN inference server that exposes
`POST /v1/chat/completions` with the standard SSE shape.

- `vllm`     — vLLM serve (`vllm serve …`), default `127.0.0.1:8000/v1`
- `lmstudio` — LM Studio's local server, default `127.0.0.1:1234/v1`
- `llamacpp` — external `llama-server` that you started yourself,
               default `127.0.0.1:8080/v1`. For the *managed* path
               (oxenClaw spawns the server itself), see the
               `llamacpp-direct` provider in `llamacpp_direct.py`.

`ollama` is registered separately in `ollama.py` against the native
`/api/chat` endpoint because Ollama's OpenAI shim silently caps
`num_ctx` at 4096 and drops tool-call deltas.
"""

from __future__ import annotations

from oxenclaw.pi.providers._openai_shared import (
    PayloadPatch,
    stream_openai_compatible,
)
from oxenclaw.pi.streaming import register_provider_stream


def _make_streamfn(payload_patch: PayloadPatch | None = None):  # type: ignore[no-untyped-def]
    async def _fn(ctx, opts):  # type: ignore[no-untyped-def]
        async for ev in stream_openai_compatible(ctx, opts, payload_patch=payload_patch):
            yield ev

    return _fn


register_provider_stream("vllm", _make_streamfn())
register_provider_stream("lmstudio", _make_streamfn())
register_provider_stream("llamacpp", _make_streamfn())
