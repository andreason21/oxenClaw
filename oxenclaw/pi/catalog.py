"""Seed model catalog.

Returns a populated `InMemoryModelRegistry` with the providers/models
oxenClaw supports out of the box. This is the Python analogue of
openclaw's `model-context-tokens` table + provider model lists.

Add models here when a new provider wrapper is registered in Phase 3.
"""

from __future__ import annotations

from oxenclaw.pi.models import Model
from oxenclaw.pi.models_dev import models_dev_enabled
from oxenclaw.pi.registry import InMemoryModelRegistry, RemoteModelRegistry


def _seed_models() -> list[Model]:
    return [
        # The catalog is on-host only. All seeded models are Ollama
        # tags — for `llamacpp-direct` / `vllm` / `lmstudio` /
        # `llamacpp` the model id is whatever string identifies the
        # operator's GGUF / vLLM weights, so there's nothing to seed.
        # ── Local / Ollama ──
        # Default. qwen3.5:9b — multimodal (vision), native function
        # calling, native thinking, 256K ctx, ~6.6 GB Q4_K_M. Picked
        # over gemma4:latest as the recommended local default after
        # the 2026-04-28 live e2e gate (18/18 PASS on PiAgent multi-
        # turn + memory-driven tool-call flow at /tmp/qwen_live_e2e.py).
        Model(
            id="qwen3.5:9b",
            provider="ollama",
            context_window=262_144,  # 256K native via rope
            max_output_tokens=8_192,
            supports_tools=True,
            supports_image_input=True,
            supports_thinking=True,
        ),
        # gemma4 family — multimodal, native function calling, 128K
        # ctx on E-class (2B/4B effective) and 256K on the 26B/31B
        # MoE variants. `gemma4:latest` aliases to e4b (~9.6 GB).
        # NOTE: there is NO `gemma4:9b` — the closest size is e4b
        # (effective 4B parameters) or 26b/31b for higher quality.
        Model(
            id="gemma4:latest",
            provider="ollama",
            context_window=131_072,  # 128K
            max_output_tokens=8_192,
            supports_tools=True,
            supports_image_input=True,
            aliases=("gemma4:e4b",),
        ),
        Model(
            id="gemma4:e2b",
            provider="ollama",
            context_window=131_072,
            max_output_tokens=8_192,
            supports_tools=True,
            supports_image_input=True,
        ),
        Model(
            id="gemma4:e4b",
            provider="ollama",
            context_window=131_072,
            max_output_tokens=8_192,
            supports_tools=True,
            supports_image_input=True,
        ),
        Model(
            id="gemma4:26b",
            provider="ollama",
            context_window=262_144,  # 256K
            max_output_tokens=8_192,
            supports_tools=True,
            supports_image_input=True,
        ),
        Model(
            id="gemma4:31b",
            provider="ollama",
            context_window=262_144,
            max_output_tokens=8_192,
            supports_tools=True,
            supports_image_input=True,
        ),
        Model(
            id="qwen2.5:7b-instruct",
            provider="ollama",
            context_window=32_768,
            max_output_tokens=4_096,
            supports_tools=True,
        ),
        Model(
            id="llama3.1:8b",
            provider="ollama",
            context_window=128_000,
            max_output_tokens=4_096,
            supports_tools=True,
        ),
        Model(
            id="mistral-nemo:12b",
            provider="ollama",
            context_window=128_000,
            max_output_tokens=4_096,
            supports_tools=True,
        ),
        Model(
            id="gemma3:4b",
            provider="ollama",
            context_window=8_192,
            max_output_tokens=2_048,
            supports_tools=False,
        ),
    ]


def default_registry() -> InMemoryModelRegistry:
    """Return the seeded registry. If `OXENCLAW_USE_MODELS_DEV=1` the
    registry is `RemoteModelRegistry`, which lazily resolves unknown
    model ids via the bundled / cached / remote models.dev catalog —
    everything in `_seed_models()` keeps working as the fast path."""
    seed = _seed_models()
    if models_dev_enabled():
        return RemoteModelRegistry(models=seed)
    return InMemoryModelRegistry(models=seed)


__all__ = ["default_registry"]
