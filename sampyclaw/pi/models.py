"""Model / Context / Api types from `@mariozechner/pi-ai`.

These are runtime value objects (not abstract bases). Concrete provider
adapters in `sampyclaw.pi.providers.*` consume `Model` + `Context` to
build payloads and call provider APIs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ProviderId = Literal[
    "anthropic",
    "anthropic-vertex",
    "openai",
    "google",
    "vertex-ai",
    "bedrock",
    "openrouter",
    "moonshot",
    "minimax",
    "zai",
    "kilocode",
    "groq",
    "deepseek",
    "mistral",
    "together",
    "fireworks",
    "ollama",
    "lmstudio",
    "vllm",
    "llamacpp",
    "litellm",
    "proxy",
    "openai-compatible",
]


@dataclass(frozen=True)
class Model:
    """Concrete model handle: id + provider + capability flags + cost shape.

    Capability flags and `context_window` mirror the upstream Model type;
    only the fields actually consumed by openclaw `pi-embedded-runner` are
    kept here. Pricing is opaque (`pricing` dict) — usage-accumulator
    knows how to multiply.
    """

    id: str
    provider: ProviderId
    context_window: int = 8192
    max_output_tokens: int = 4096
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_thinking: bool = False
    supports_image_input: bool = False
    supports_prompt_cache: bool = False
    pricing: dict[str, float] | None = None
    aliases: tuple[str, ...] = ()
    # Extra provider-specific knobs (e.g. ollama num_predict alias).
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Api:
    """Endpoint + auth descriptor for a provider call."""

    base_url: str
    api_key: str | None = None
    organization: str | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Context:
    """Per-attempt context bundle handed to streamSimple / event stream."""

    model: Model
    api: Api
    system: str | None = None
    # User/assistant/tool_result messages in order; runtime adapters lift
    # the leading `system` if their provider wants it as a top-level field.
    messages: list[Any] = field(default_factory=list)
    tools: list[Any] = field(default_factory=list)
    temperature: float = 0.0
    max_tokens: int | None = None
    stop_sequences: tuple[str, ...] = ()
    thinking: dict[str, Any] | None = None
    cache_control_breakpoints: int = 1
    extra: dict[str, Any] = field(default_factory=dict)


__all__ = ["Api", "Context", "Model", "ProviderId"]
