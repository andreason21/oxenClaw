"""Token estimation + model context table.

Mirrors `@mariozechner/pi-coding-agent` `estimateTokens` and the
`model-context-tokens` map openclaw uses to budget compaction.

We use `tiktoken` for OpenAI-family precision when available; otherwise
fall back to a calibrated `len(text) / 3.5` heuristic that openclaw's
TS path uses for non-tiktoken-supported providers. The fallback is
intentionally pessimistic (slightly over-counts) so the compaction
trigger fires a hair earlier rather than a hair too late.

estimate_tokens accepts:
- a `str`: token count of the string
- a `list[AgentMessage]`: sum of role overhead + content tokens

The role overhead constants (~4 tokens per turn) are the same OpenAI
publishes for chat-format completions and roughly match Anthropic /
Google in practice.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

# Per-message overhead. OpenAI's chat-format docs cite 4 tokens; Anthropic
# and Google are similar in practice. Slightly conservative.
_PER_MESSAGE_OVERHEAD = 4
# Char→token ratio for the fallback path. 3.5 chars/token is the empirical
# average for English mixed code/prose. CJK skews lower (~2 chars/token);
# we accept the over-estimate there.
_CHAR_PER_TOKEN_FALLBACK = 3.5

# Optional tiktoken; cached encoder.
_ENCODER: Any | None = None
_ENCODER_TRIED = False


def _get_encoder():  # type: ignore[no-untyped-def]
    """Lazy-load and cache tiktoken's cl100k_base encoder. Returns None if
    tiktoken isn't installed (we fall back to char-count heuristic)."""
    global _ENCODER, _ENCODER_TRIED
    if _ENCODER is not None or _ENCODER_TRIED:
        return _ENCODER
    _ENCODER_TRIED = True
    try:
        import tiktoken

        _ENCODER = tiktoken.get_encoding("cl100k_base")
    except ImportError:
        _ENCODER = None
    return _ENCODER


def estimate_tokens_for_text(text: str) -> int:
    """Token count for a single string."""
    if not text:
        return 0
    enc = _get_encoder()
    if enc is not None:
        return len(enc.encode(text, disallowed_special=()))
    return max(1, round(len(text) / _CHAR_PER_TOKEN_FALLBACK))


def estimate_tokens(value: str | list[Any] | dict[str, Any]) -> int:
    """Estimate token count for text, message lists, or arbitrary JSON.

    For a list of `AgentMessage`-shaped objects (pydantic models or dicts),
    we sum role overhead plus content tokens. For unknown shapes, we
    serialise to JSON and use the fallback heuristic — guarantees we
    never under-count by more than the JSON overhead.
    """
    if isinstance(value, str):
        return estimate_tokens_for_text(value)
    if isinstance(value, list):
        total = 0
        for item in value:
            total += _PER_MESSAGE_OVERHEAD + _estimate_message_tokens(item)
        return total
    if isinstance(value, dict):
        return estimate_tokens_for_text(json.dumps(value, ensure_ascii=False))
    return 0


def _estimate_message_tokens(msg: Any) -> int:
    """Inspect a single AgentMessage-shaped object and tally tokens."""
    if hasattr(msg, "model_dump"):
        msg = msg.model_dump()
    if not isinstance(msg, dict):
        return estimate_tokens_for_text(str(msg))
    content = msg.get("content")
    if isinstance(content, str):
        return estimate_tokens_for_text(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            total += _estimate_block_tokens(block)
        return total
    # tool_result message: results list.
    results = msg.get("results")
    if isinstance(results, list):
        total = 0
        for r in results:
            inner = r.get("content") if isinstance(r, dict) else None
            if isinstance(inner, str):
                total += estimate_tokens_for_text(inner)
            elif isinstance(inner, list):
                for blk in inner:
                    total += _estimate_block_tokens(blk)
        return total
    return 0


def _estimate_block_tokens(block: Any) -> int:
    if isinstance(block, dict):
        block_type = block.get("type")
        if block_type == "text":
            return estimate_tokens_for_text(block.get("text", ""))
        if block_type == "thinking":
            return estimate_tokens_for_text(block.get("thinking", ""))
        if block_type == "tool_use":
            return estimate_tokens_for_text(
                json.dumps(block.get("input", {}), ensure_ascii=False)
            ) + estimate_tokens_for_text(block.get("name", ""))
        if block_type == "image":
            # Anthropic charges roughly 1.6 tokens per image tile of
            # 1024×1024 — without dimensions, use a flat ballpark.
            return 1500
    return estimate_tokens_for_text(str(block))


# ─── Model context window table ──────────────────────────────────────


# Subset of openclaw's table — extend as providers are added. Keys are
# canonical model ids OR aliases. Compaction code looks up `model.id` then
# `model.aliases` against this map; missing → conservative 8K.
MODEL_CONTEXT_TOKENS: dict[str, int] = {
    # Anthropic
    "claude-sonnet-4-6": 1_000_000,
    "claude-sonnet-4-5": 200_000,
    "claude-opus-4-7": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "o1": 200_000,
    "o3": 200_000,
    # Google
    "gemini-1.5-pro": 2_000_000,
    "gemini-2.0-flash": 1_000_000,
    "gemini-2.5-pro": 2_000_000,
    # Local (Ollama defaults)
    # gemma4: 128K on e2b/e4b/latest, 256K on 26b/31b MoE.
    "gemma4:latest": 131_072,
    "gemma4:e2b": 131_072,
    "gemma4:e4b": 131_072,
    "gemma4:26b": 262_144,
    "gemma4:31b": 262_144,
    "qwen2.5:7b-instruct": 32_768,
    "llama3.1:8b": 128_000,
    "mistral-nemo:12b": 128_000,
    "gemma3:4b": 8_192,
    "deepseek-r1:7b": 32_768,
}


def model_context_window(model_id: str, *, default: int = 8_192) -> int:
    """Look up a model's context window in tokens. Falls back to `default`."""
    return MODEL_CONTEXT_TOKENS.get(model_id, default)


__all__ = [
    "MODEL_CONTEXT_TOKENS",
    "estimate_tokens",
    "estimate_tokens_for_text",
    "model_context_window",
]
