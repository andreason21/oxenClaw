"""Seed model catalog.

Returns a populated `InMemoryModelRegistry` with the providers/models
sampyClaw supports out of the box. This is the Python analogue of
openclaw's `model-context-tokens` table + provider model lists.

Add models here when a new provider wrapper is registered in Phase 3.
"""

from __future__ import annotations

from sampyclaw.pi.models import Model
from sampyclaw.pi.registry import InMemoryModelRegistry


def default_registry() -> InMemoryModelRegistry:
    return InMemoryModelRegistry(
        models=[
            # ── Anthropic ──
            Model(
                id="claude-opus-4-7",
                provider="anthropic",
                context_window=200_000,
                max_output_tokens=32_000,
                supports_thinking=True,
                supports_image_input=True,
                supports_prompt_cache=True,
                aliases=("claude-opus-latest",),
            ),
            Model(
                id="claude-sonnet-4-6",
                provider="anthropic",
                context_window=1_000_000,
                max_output_tokens=64_000,
                supports_thinking=True,
                supports_image_input=True,
                supports_prompt_cache=True,
                aliases=("claude-sonnet-1m",),
            ),
            Model(
                id="claude-haiku-4-5-20251001",
                provider="anthropic",
                context_window=200_000,
                max_output_tokens=8_192,
                supports_thinking=False,
                supports_image_input=True,
                supports_prompt_cache=True,
                aliases=("claude-haiku-4-5", "claude-haiku-latest"),
            ),
            # ── OpenAI ──
            Model(
                id="gpt-4o",
                provider="openai",
                context_window=128_000,
                max_output_tokens=16_384,
                supports_image_input=True,
                supports_prompt_cache=True,
            ),
            Model(
                id="gpt-4o-mini",
                provider="openai",
                context_window=128_000,
                max_output_tokens=16_384,
                supports_image_input=True,
                supports_prompt_cache=True,
            ),
            Model(
                id="o3",
                provider="openai",
                context_window=200_000,
                max_output_tokens=100_000,
                supports_thinking=True,
            ),
            # ── Google ──
            Model(
                id="gemini-2.5-pro",
                provider="google",
                context_window=2_000_000,
                max_output_tokens=64_000,
                supports_thinking=True,
                supports_image_input=True,
                supports_prompt_cache=True,
            ),
            Model(
                id="gemini-2.0-flash",
                provider="google",
                context_window=1_000_000,
                max_output_tokens=8_192,
                supports_image_input=True,
            ),
            # ── Local / Ollama ──
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
    )


__all__ = ["default_registry"]
