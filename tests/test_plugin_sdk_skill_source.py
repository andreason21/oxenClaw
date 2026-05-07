"""SkillSourcePlugin protocol + demo plugin smoke tests.

Locks two things:

  1. The protocol matches what `ClawHubClient` already exposes — if
     someone adds a method to ClawHubClient and forgets to update the
     protocol (or vice versa), `isinstance(c, SkillSourcePlugin)` keeps
     working only as long as the four required methods stay aligned.

  2. The bundled demo source actually round-trips a fetch — search →
     list → detail → download → integrity-check — so plugin authors
     can copy this module pattern with confidence.
"""

from __future__ import annotations

import hashlib

from oxenclaw.clawhub.client import ClawHubClient, sha256_integrity
from oxenclaw.extensions.skill_source_demo.source import DemoSkillSource
from oxenclaw.plugin_sdk.skill_source_contract import (
    SKILL_SOURCE_ENTRY_POINT_GROUP,
    SkillSourcePlugin,
)


# ─── protocol ──────────────────────────────────────────────────────


def test_clawhub_client_satisfies_protocol() -> None:
    """ClawHubClient is the canonical implementation; if it ever drifts
    from the protocol the rest of the plumbing breaks at boot for every
    non-plugin install. The runtime_checkable Protocol catches that
    drift here."""
    client = ClawHubClient()
    assert isinstance(client, SkillSourcePlugin)


def test_demo_source_satisfies_protocol() -> None:
    src = DemoSkillSource(options={})
    assert isinstance(src, SkillSourcePlugin)


def test_entry_point_group_constant_matches_pyproject() -> None:
    """Drift guard: the constant the loader uses to look up plugins
    must match the literal group name in pyproject.toml. Hardcoded
    string here so a typo in the constant gets caught even on a
    machine where the demo isn't installed."""
    assert SKILL_SOURCE_ENTRY_POINT_GROUP == "oxenclaw.skill_sources"


# ─── demo round-trip ────────────────────────────────────────────────


async def test_demo_search_returns_demo_skill() -> None:
    src = DemoSkillSource()
    hits = await src.search_skills("demo")
    assert any(h["slug"] == "demo-skill" for h in hits)


async def test_demo_search_empty_query_returns_full_catalog() -> None:
    src = DemoSkillSource(options={"extra_skills": {"x": {"slug": "x", "version": "0.1"}}})
    hits = await src.search_skills("")
    assert {h["slug"] for h in hits} == {"demo-skill", "x"}


async def test_demo_list_skills_returns_results_dict() -> None:
    src = DemoSkillSource()
    out = await src.list_skills()
    assert "results" in out and out["results"][0]["slug"] == "demo-skill"


async def test_demo_fetch_detail_carries_latest_version() -> None:
    src = DemoSkillSource()
    detail = await src.fetch_skill_detail("demo-skill")
    assert detail["latestVersion"]["version"] == "1.0.0"


async def test_demo_fetch_detail_unknown_slug_raises() -> None:
    src = DemoSkillSource()
    try:
        await src.fetch_skill_detail("nope")
    except KeyError as exc:
        assert "nope" in str(exc)
    else:
        raise AssertionError("expected KeyError for unknown slug")


async def test_demo_download_archive_integrity_matches() -> None:
    """The integrity string the source returns must be the canonical
    sha256 of the archive — `SkillInstaller` compares the two and
    aborts on mismatch."""
    src = DemoSkillSource()
    archive, integrity = await src.download_skill_archive("demo-skill")
    expected = sha256_integrity(archive)
    assert integrity == expected
    # Also verify the hash is computed over the actual bytes (not
    # padded / truncated).
    raw_hex = hashlib.sha256(archive).hexdigest()
    assert integrity == f"sha256-{raw_hex}"


async def test_demo_aclose_does_not_raise() -> None:
    src = DemoSkillSource()
    await src.aclose()  # protocol contract: must be safely no-op
