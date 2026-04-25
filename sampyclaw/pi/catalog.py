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
