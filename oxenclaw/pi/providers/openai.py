"""OpenAI + every OpenAI-compatible inline provider.

A single shared SSE wrapper handles: openai, ollama, lmstudio, vllm,
llamacpp, litellm, openai-compatible, proxy. Per-provider tweaks live
alongside the registration as `payload_patch` callables.
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


# Plain OpenAI / OpenAI-compat providers — no payload tweak.
register_provider_stream("openai", _make_streamfn())
register_provider_stream("openai-compatible", _make_streamfn())
register_provider_stream("proxy", _make_streamfn())
register_provider_stream("vllm", _make_streamfn())
register_provider_stream("llamacpp", _make_streamfn())
register_provider_stream("litellm", _make_streamfn())
register_provider_stream("lmstudio", _make_streamfn())
register_provider_stream("groq", _make_streamfn())
register_provider_stream("deepseek", _make_streamfn())
register_provider_stream("mistral", _make_streamfn())
register_provider_stream("together", _make_streamfn())
register_provider_stream("fireworks", _make_streamfn())
register_provider_stream("kilocode", _make_streamfn())

# Note: provider id "ollama" is registered by `oxenclaw.pi.providers.ollama`
# against the native /api/chat endpoint. The OpenAI shim path is unusable
# for Ollama because it silently caps num_ctx at 4096 regardless of
# `options.num_ctx`, truncating memory + skill manifests.
