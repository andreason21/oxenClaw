"""Maximal Marginal Relevance (MMR) re-ranking.

Mirrors openclaw `extensions/memory-core/src/memory/mmr.ts`. MMR balances
relevance with diversity by iteratively selecting items maximising
``lambda * relevance - (1 - lambda) * max_similarity_to_selected``.

The tokenizer is CJK-aware: it produces ASCII alnum tokens, CJK unigrams,
and bigrams across CJK characters that were adjacent in the original text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from oxenclaw.memory.models import MemorySearchResult

# CJK-family unicode ranges (no whitespace word boundaries):
#   Hiragana U+3040-309F, Katakana U+30A0-30FF,
#   CJK Extension A U+3400-4DBF, CJK Unified Ideographs U+4E00-9FFF,
#   Hangul Syllables U+AC00-D7AF, Hangul Jamo U+1100-11FF.
_CJK_RE = re.compile(
    "["
    "぀-ゟ"  # Hiragana
    "゠-ヿ"  # Katakana
    "㐀-䶿"  # CJK Extension A
    "一-鿿"  # CJK Unified Ideographs
    "가-힯"  # Hangul Syllables
    "ᄀ-ᇿ"  # Hangul Jamo
    "]"
)
_ASCII_RE = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True)
class MMRConfig:
    """MMR knobs. ``lambda_`` of 1.0 = pure relevance, 0.0 = pure diversity."""

    enabled: bool = False
    lambda_: float = 0.7


DEFAULT_MMR_CONFIG = MMRConfig()


def tokenize(text: str) -> set[str]:
    """ASCII alnum tokens + CJK unigrams + adjacent-CJK bigrams.

    Bigrams are only emitted from CJK characters that were adjacent in the
    original text, so mixed content like ``"我喜欢hello你好"`` will not
    produce the spurious bigram ``"欢你"``.
    """
    lower = text.lower()
    ascii_tokens = _ASCII_RE.findall(lower)

    cjk_data: list[tuple[str, int]] = []
    for i, ch in enumerate(lower):
        if _CJK_RE.match(ch):
            cjk_data.append((ch, i))

    bigrams: list[str] = []
    for i in range(len(cjk_data) - 1):
        if cjk_data[i + 1][1] == cjk_data[i][1] + 1:
            bigrams.append(cjk_data[i][0] + cjk_data[i + 1][0])

    unigrams = [c for c, _ in cjk_data]
    return set(ascii_tokens) | set(bigrams) | set(unigrams)


def jaccard_similarity(a: set[str], b: set[str]) -> float:
    """Jaccard. Both empty → 1.0; one empty → 0.0; else |∩| / |∪|."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    smaller, larger = (a, b) if len(a) <= len(b) else (b, a)
    intersection = sum(1 for tok in smaller if tok in larger)
    union = len(a) + len(b) - intersection
    return 0.0 if union == 0 else intersection / union


def text_similarity(a: str, b: str) -> float:
    """Jaccard on tokenised content."""
    return jaccard_similarity(tokenize(a), tokenize(b))


def _max_similarity(
    candidate_tokens: set[str],
    selected_token_lists: list[set[str]],
) -> float:
    if not selected_token_lists:
        return 0.0
    best = 0.0
    for sel in selected_token_lists:
        sim = jaccard_similarity(candidate_tokens, sel)
        if sim > best:
            best = sim
    return best


def mmr_rerank(
    items: list[MemorySearchResult],
    *,
    config: MMRConfig = DEFAULT_MMR_CONFIG,
) -> list[MemorySearchResult]:
    """Iteratively pick items maximising MMR objective. Re-emits originals.

    Algorithm: incremental MMR. Maintain `max_sim_to_selected[i]` for every
    candidate; after each pick `s`, update with `max(prev, sim(i, s))`. This
    avoids recomputing K Jaccards per candidate per round and avoids the
    O(N) `list.pop()` call. Net cost: O(N²) tokenization-bounded similarity
    work, vs. the prior O(N² × K) with K = current selection size.
    """
    if not config.enabled or len(items) <= 1:
        return list(items)

    lam = max(0.0, min(1.0, config.lambda_))

    if lam == 1.0:
        return sorted(items, key=lambda r: r.score, reverse=True)

    n = len(items)
    tokens: list[set[str]] = [tokenize(item.chunk.text) for item in items]

    scores = [it.score for it in items]
    max_score = max(scores)
    min_score = min(scores)
    score_range = max_score - min_score
    relevance = [1.0 if score_range == 0 else (s - min_score) / score_range for s in scores]

    chosen: list[bool] = [False] * n
    max_sim: list[float] = [0.0] * n
    selected_order: list[int] = []

    for _ in range(n):
        best_idx = -1
        best_mmr = float("-inf")
        best_score = float("-inf")
        for i in range(n):
            if chosen[i]:
                continue
            mmr = lam * relevance[i] - (1.0 - lam) * max_sim[i]
            # Tie-break on raw score — preserves the prior behavior so unit
            # tests asserting deterministic order keep passing.
            if mmr > best_mmr or (mmr == best_mmr and scores[i] > best_score):
                best_mmr = mmr
                best_idx = i
                best_score = scores[i]
        if best_idx < 0:
            break
        chosen[best_idx] = True
        selected_order.append(best_idx)
        # Incremental update: only the newly selected item can raise sim.
        sel_tokens = tokens[best_idx]
        for j in range(n):
            if chosen[j]:
                continue
            sim = jaccard_similarity(tokens[j], sel_tokens)
            if sim > max_sim[j]:
                max_sim[j] = sim

    return [items[i] for i in selected_order]
