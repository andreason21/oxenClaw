"""Capability gate: does a given model accept image inputs?

Lookup priority:

1. The pi catalog (`sampyclaw.pi.catalog.default_registry`) — authoritative
   for any model the runtime knows about (`Model.supports_image_input`).
2. A hard-coded set of well-known multimodal models for cases where a
   non-pi agent is talking to a provider directly (e.g. `LocalAgent`
   pointed at an Ollama tag that isn't in the catalog yet, or the plain
   `AnthropicAgent` constructed without a pi `Model` handle).

The hard-coded list errs on the side of permissive — it's used to *opt
in* image dispatch, not to block providers that have already accepted
images on the wire. False negatives degrade gracefully (image is
dropped + text fallback). False positives are caught by the provider's
own validation.
"""

from __future__ import annotations

# Lower-cased name fragments — `model_supports_images` lower-cases the
# input model id and looks for any of these as a substring. Keep entries
# specific enough that they don't accidentally match unrelated models.
KNOWN_IMAGE_MODELS: tuple[str, ...] = (
    # Anthropic Claude — every Claude 3+ generation supports images.
    "claude-3",
    "claude-sonnet-4",
    "claude-opus-4",
    "claude-haiku-4",
    "claude-sonnet-3-5",
    # OpenAI GPT-4o family + o1-vision.
    "gpt-4o",
    "gpt-4-turbo",
    "gpt-4-vision",
    "gpt-5",
    # Google Gemini — all 1.5+ tiers support images.
    "gemini-1.5",
    "gemini-2",
    "gemini-2.5",
    # Local: gemma3 vision and gemma4 family.
    "gemma3:4b",
    "gemma3:12b",
    "gemma3:27b",
    "gemma4",
    # llama 3.2 vision.
    "llama3.2-vision",
    "llava",
    "moondream",
    "minicpm-v",
    "qwen2.5vl",
    "qwen2-vl",
)


def model_supports_images(model_id: str | None) -> bool:
    """Best-effort image-input capability check.

    Empty / None → False. Otherwise consults the pi catalog first, and
    falls back to substring match against `KNOWN_IMAGE_MODELS`.
    """
    if not model_id:
        return False
    name = model_id.lower()

    # 1. Catalog (authoritative for cataloged models).
    try:
        from sampyclaw.pi.catalog import default_registry

        registry = default_registry()
        model = registry.get(model_id)
        if model is not None:
            return bool(model.supports_image_input)
    except Exception:
        # Catalog import shouldn't fail in practice, but a missing dep
        # must not fail the gate — fall through to the heuristic.
        pass

    # 2. Heuristic for models the catalog doesn't know.
    return any(token in name for token in KNOWN_IMAGE_MODELS)


__all__ = ["KNOWN_IMAGE_MODELS", "model_supports_images"]
