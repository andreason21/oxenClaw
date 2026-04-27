"""Tests for wiki claims — add_claim / verify_claim / find_contradictions."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from oxenclaw.wiki.claims import add_claim, find_contradictions, verify_claim
from oxenclaw.wiki.models import WikiClaim, WikiEvidence, WikiPage, WikiPageKind


def _empty_page() -> WikiPage:
    return WikiPage(kind=WikiPageKind.CONCEPT, name="Test", slug="test", body="")


# ─── add_claim ───────────────────────────────────────────────────────


def test_add_claim_appends_to_page() -> None:
    page = _empty_page()
    new_page, claim = add_claim(page, "Water boils at 100°C.")
    assert len(new_page.claims) == 1
    assert new_page.claims[0].text == "Water boils at 100°C."
    # Original page must be untouched (dataclass is mutable but
    # add_claim returns a new instance).
    assert len(page.claims) == 0


def test_add_claim_sets_asserted_at() -> None:
    before = time.time()
    _, claim = add_claim(_empty_page(), "Claim text")
    assert claim.asserted_at is not None
    assert claim.asserted_at >= before


def test_add_claim_generates_claim_id() -> None:
    _, claim = add_claim(_empty_page(), "Some claim")
    assert claim.claim_id is not None
    assert len(claim.claim_id) == 8


def test_add_claim_with_evidence() -> None:
    ev = WikiEvidence(source_id="src-1", note="page 5")
    _, claim = add_claim(_empty_page(), "Backed claim", evidence=[ev])
    assert len(claim.evidence) == 1
    assert claim.evidence[0].source_id == "src-1"


def test_add_claim_with_confidence() -> None:
    _, claim = add_claim(_empty_page(), "Confident claim", confidence=0.9)
    assert claim.confidence == pytest.approx(0.9)


def test_add_claim_accumulates() -> None:
    page = _empty_page()
    page, _ = add_claim(page, "First")
    page, _ = add_claim(page, "Second")
    assert len(page.claims) == 2


def test_add_claim_updates_page_updated_at() -> None:
    page = _empty_page()
    before = page.updated_at
    time.sleep(0.01)
    new_page, _ = add_claim(page, "New claim")
    assert new_page.updated_at >= before


# ─── verify_claim ────────────────────────────────────────────────────


def test_verify_claim_sets_last_verified_at() -> None:
    page = _empty_page()
    page, claim = add_claim(page, "Verifiable claim")
    assert claim.last_verified_at is None
    before = time.time()
    verified_page = verify_claim(page, claim.claim_id)
    matched = next(
        (c for c in verified_page.claims if c.claim_id == claim.claim_id), None
    )
    assert matched is not None
    assert matched.last_verified_at is not None
    assert matched.last_verified_at >= before


def test_verify_claim_unknown_id_returns_unchanged() -> None:
    page = _empty_page()
    page, _ = add_claim(page, "Some claim")
    result = verify_claim(page, "deadbeef")
    # Claims unchanged — nothing matched.
    assert result.claims == page.claims


def test_verify_claim_only_touches_matching_claim() -> None:
    page = _empty_page()
    page, c1 = add_claim(page, "Claim A")
    page, c2 = add_claim(page, "Claim B")
    result = verify_claim(page, c1.claim_id)
    a = next(c for c in result.claims if c.claim_id == c1.claim_id)
    b = next(c for c in result.claims if c.claim_id == c2.claim_id)
    assert a.last_verified_at is not None
    assert b.last_verified_at is None


# ─── find_contradictions ─────────────────────────────────────────────


def test_find_contradictions_no_claims_returns_empty() -> None:
    assert find_contradictions(_empty_page()) == []


def test_find_contradictions_detects_negation_pair() -> None:
    """Two claims with high overlap where one negates the other."""
    page = _empty_page()
    page, _ = add_claim(page, "The system is stable and running correctly.")
    page, _ = add_claim(page, "The system is not stable and not running correctly.")
    pairs = find_contradictions(page)
    assert len(pairs) >= 1
    texts = {c.text for pair in pairs for c in pair}
    assert any("not" in t for t in texts)


def test_find_contradictions_unrelated_claims_are_not_paired() -> None:
    page = _empty_page()
    page, _ = add_claim(page, "Python was created by Guido van Rossum.")
    page, _ = add_claim(page, "Photosynthesis converts sunlight into energy.")
    pairs = find_contradictions(page)
    assert pairs == []


def test_find_contradictions_no_negation_no_contradiction() -> None:
    """High overlap but no negation marker — should not flag."""
    page = _empty_page()
    page, _ = add_claim(page, "The API returns JSON with a status field.")
    page, _ = add_claim(page, "The API returns JSON with a status and data field.")
    pairs = find_contradictions(page)
    # No negation markers, so no contradictions.
    assert pairs == []


def test_find_contradictions_single_claim_returns_empty() -> None:
    page = _empty_page()
    page, _ = add_claim(page, "Only claim here.")
    assert find_contradictions(page) == []
