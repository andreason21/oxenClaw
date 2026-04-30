"""token_estimator + vision_keep_turns_for unit tests."""

from __future__ import annotations

from oxenclaw.pi.run.history_image_prune import vision_keep_turns_for
from oxenclaw.pi.run.token_estimator import chars_per_token_for, estimate_tokens


def test_family_ratios() -> None:
    assert chars_per_token_for("qwen3.5:9b") == 2.5
    assert chars_per_token_for("claude-sonnet-4-6") == 3.0
    assert chars_per_token_for("gemma4:latest") == 2.8
    assert chars_per_token_for("llama3.1:8b") == 3.0
    assert chars_per_token_for("mistral-nemo:12b") == 3.0
    assert chars_per_token_for("phi-3-medium") == 3.0
    assert chars_per_token_for("deepseek-coder:6.7b") == 2.8


def test_default_ratio_when_unknown() -> None:
    assert chars_per_token_for(None) == 3.5
    assert chars_per_token_for("unknown-model") == 3.5
    assert chars_per_token_for("") == 3.5


def test_estimate_tokens_basics() -> None:
    assert estimate_tokens("") == 0
    # gemma ratio 2.8 → 280 chars ≈ 100 tokens
    assert estimate_tokens("a" * 280, model_id="gemma4:e4b") == 100
    # qwen ratio 2.5 → 100 chars ≈ 40 tokens
    assert estimate_tokens("a" * 100, model_id="qwen3.5:9b") == 40
    # default 3.5 → 350 chars ≈ 100 tokens
    assert estimate_tokens("a" * 350, model_id=None) == 100


def test_estimate_tokens_minimum_1() -> None:
    # Even tiny strings should report ≥1 token (matches the
    # legacy `_estimate_tokens_from_text` floor).
    assert estimate_tokens("x", model_id="qwen3.5:9b") == 1


def test_vision_keep_turns_family() -> None:
    assert vision_keep_turns_for("claude-sonnet-4-6") == 6
    assert vision_keep_turns_for("gpt-4o") == 6
    assert vision_keep_turns_for("qwen3.5:9b") == 4
    assert vision_keep_turns_for("gemma4:e4b") == 4
    assert vision_keep_turns_for("llava:7b") == 3
    assert vision_keep_turns_for("anthropic.claude-3-5") == 6


def test_vision_keep_turns_default() -> None:
    assert vision_keep_turns_for(None) == 2
    assert vision_keep_turns_for("nonexistent-model") == 2
    assert vision_keep_turns_for("") == 2
