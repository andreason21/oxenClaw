"""Claim manipulation helpers — add, verify, contradict.

Mirrors openclaw `memory-wiki/src/claim-health.ts`.

All operations work on *immutable* dataclasses — every function returns a
**new** ``WikiPage`` (or ``WikiClaim``) instance; the caller is responsible
for persisting it via the vault.

Claim IDs
---------
``add_claim`` generates a stable 8-hex ``claim_id`` derived from the
claim text and the current timestamp so the ID changes when the text
changes (intentional — a reworded claim is a new claim).

Contradiction detection
-----------------------
``find_contradictions`` is a *best-effort* heuristic:

1. Two claims are candidate-contradictory when their normalised text has
   ≥ 40 % token overlap.
2. At least one claim must contain a negation marker (``not``, ``never``,
   ``no longer``, ``false``, ``incorrect``, ``wrong``).

.. note::
    TODO: replace with LLM-driven contradiction detection in a future
    session.  The heuristic is intentionally loose — it prefers false
    positives over missed contradictions.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import replace

from oxenclaw.wiki.models import WikiClaim, WikiEvidence, WikiPage

# Tokens that indicate a claim might be negating something.
_NEGATION_TOKENS: frozenset[str] = frozenset(
    {"not", "never", "no", "no longer", "false", "incorrect", "wrong", "none"}
)

# Minimum Jaccard-like overlap to consider two claims as potentially
# covering the same topic.
_OVERLAP_THRESHOLD = 0.40


def _make_claim_id(text: str, ts: float) -> str:
    raw = f"{text}|{ts:.3f}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def _tokenise(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _overlap_ratio(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _has_negation(text: str) -> bool:
    lower = text.lower()
    return any(marker in lower for marker in _NEGATION_TOKENS)


def add_claim(
    page: WikiPage,
    text: str,
    evidence: list[WikiEvidence] | None = None,
    confidence: float | None = None,
) -> tuple[WikiPage, WikiClaim]:
    """Append a new claim to ``page``.

    Parameters
    ----------
    page:
        The page to mutate (a fresh dataclass instance is returned).
    text:
        The claim text.
    evidence:
        Optional list of evidence items backing the claim.
    confidence:
        Optional 0.0–1.0 confidence score.

    Returns
    -------
    (new_page, new_claim)
        ``new_page`` is a fresh ``WikiPage`` instance with the claim
        appended; ``new_claim`` is the created ``WikiClaim``.
    """
    now = time.time()
    claim_id = _make_claim_id(text, now)
    new_claim = WikiClaim(
        text=text,
        evidence=tuple(evidence or []),
        confidence=confidence,
        asserted_at=now,
        last_verified_at=None,
        claim_id=claim_id,
    )
    new_page = replace(
        page,
        claims=(*page.claims, new_claim),
        updated_at=now,
    )
    return new_page, new_claim


def verify_claim(page: WikiPage, claim_id: str) -> WikiPage:
    """Mark the claim identified by ``claim_id`` as verified *now*.

    If no claim matches ``claim_id`` the page is returned unchanged.
    """
    now = time.time()
    new_claims = tuple(
        replace(c, last_verified_at=now) if getattr(c, "claim_id", None) == claim_id else c
        for c in page.claims
    )
    return replace(page, claims=new_claims, updated_at=now)


def find_contradictions(page: WikiPage) -> list[tuple[WikiClaim, WikiClaim]]:
    """Return pairs of claims that appear to contradict each other.

    A pair ``(a, b)`` is returned when:
    - Their token-overlap ratio ≥ ``_OVERLAP_THRESHOLD``.
    - At least one contains a negation marker.

    The list contains each pair at most once (``a < b`` ordering by
    claim text to avoid duplicates).

    .. note::
        TODO: replace with LLM-driven detection in a future session.
    """
    claims = list(page.claims)
    pairs: list[tuple[WikiClaim, WikiClaim]] = []
    for i, a in enumerate(claims):
        tok_a = _tokenise(a.text)
        for b in claims[i + 1 :]:
            tok_b = _tokenise(b.text)
            if _overlap_ratio(tok_a, tok_b) < _OVERLAP_THRESHOLD:
                continue
            if _has_negation(a.text) or _has_negation(b.text):
                pairs.append((a, b))
    return pairs


__all__ = ["add_claim", "find_contradictions", "verify_claim"]
