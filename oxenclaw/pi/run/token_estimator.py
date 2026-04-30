"""Model-aware token-count estimator.

oxenClaw original. openclaw uses a single
`ESTIMATED_CHARS_PER_TOKEN = 4` constant
(`src/agents/pi-embedded-runner/run/preemptive-compaction.ts`); we
deliberately diverge to a per-family table so the preemptive
compactor doesn't trigger spurious truncations on Korean sessions
(qwen / llama tokenizers count multi-byte CJK chars more densely)
and doesn't underestimate near the budget on gemma's aggressive
SentencePiece splits.

If `tiktoken` is installed and the model id matches a known tokenizer,
we use real token counts. Otherwise we fall back to the per-family
ratio.
"""

from __future__ import annotations

from typing import Final

# Default fallback ratio when no family pattern matches. Same value
# (3.5) the legacy oxenClaw estimator used pre-rc.21.
_DEFAULT_RATIO: Final[float] = 3.5

# Family-specific ratios. Empirically tuned for KO+EN mixed sessions
# we actually run; not derived from any upstream tokenizer dump.
# - Anthropic SentencePiece: ~3.5 chars/token English, ~1.8 Korean. Average ~3.0.
# - Qwen tokenizer (BBPE): ~3.0 English, ~1.5 Korean. Average ~2.5.
# - Llama-3 tiktoken: ~3.5 English, ~2.0 Korean. Average ~3.0.
# - Gemma SentencePiece: ~2.8 English (aggressive splits), ~1.6 Korean.
# - Multimodal with images: each image ~250-1500 tokens; we don't try
#   to estimate inline base64 — caller should prune images first.
_FAMILY_RATIOS: dict[str, float] = {
    "anthropic": 3.0,
    "claude": 3.0,
    "qwen": 2.5,
    "llama": 3.0,
    "gemma": 2.8,
    "mistral": 3.0,
    "phi": 3.0,
    "deepseek": 2.8,
    "default": _DEFAULT_RATIO,
}


def chars_per_token_for(model_id: str | None) -> float:
    """Pick the chars/token ratio for a model id.

    Lowercase substring match against the family table. Falls back to
    `_DEFAULT_RATIO` when nothing matches.
    """
    if not model_id:
        return _DEFAULT_RATIO
    mid = model_id.lower()
    for family, ratio in _FAMILY_RATIOS.items():
        if family == "default":
            continue
        if family in mid:
            return ratio
    return _DEFAULT_RATIO


def estimate_tokens(text: str, *, model_id: str | None = None) -> int:
    """Estimate token count for `text` using the right ratio for the model.

    Tries `tiktoken` first when available + the model id maps to a
    known tokenizer. On any failure, falls back to char/ratio division.
    """
    if not text:
        return 0
    # Best-effort tiktoken path. We only use it for tiktoken-native
    # models (OpenAI / llama-3). Anthropic and Qwen don't ship tiktoken
    # encoders so we deliberately skip those.
    if model_id:
        mid = model_id.lower()
        if "gpt" in mid or "o1" in mid or "o3" in mid or "o4" in mid:
            try:
                import tiktoken  # type: ignore[import-untyped]

                enc = tiktoken.encoding_for_model(mid)
                return len(enc.encode(text))
            except Exception:
                pass
    ratio = chars_per_token_for(model_id)
    return max(1, int(len(text) / ratio))


__all__ = ["chars_per_token_for", "estimate_tokens"]
