"""skills.* RPCs annotate every entry with `compat` and (by default)
hide entries that fail the environment-compatibility probe.

Locks the contract: the dashboard's catalog never has to tell the
operator "you can't install this" — it just doesn't show it. With
`include_incompatible: true` the full set comes back annotated so
the user can opt into the noisier view.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from oxenclaw.clawhub.client import ClawHubClient
from oxenclaw.clawhub.installer import SkillInstaller
from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.gateway.router import Router
from oxenclaw.gateway.skills_methods import register_skills_methods


def _payload_compatible() -> dict:
    return {
        "slug": "weather-cli",
        "displayName": "Weather CLI",
        "openclaw": {"os": ["linux", "darwin"], "requires": {"bins": []}},
    }


def _payload_wrong_os() -> dict:
    return {
        "slug": "mac-only",
        "displayName": "Mac-only stock skill",
        "openclaw": {"os": ["darwin"], "requires": {}},
    }


def _payload_missing_bin() -> dict:
    return {
        "slug": "needs-foo",
        "displayName": "Needs foo CLI",
        "openclaw": {"requires": {"bins": ["definitely-not-installed-xyz"]}},
    }


def _payload_no_metadata() -> dict:
    """Search summary often has only slug + name. Must pass through
    by default since we can't prove it's incompatible."""
    return {"slug": "unknown", "displayName": "Unknown skill"}


@pytest.fixture()
def setup(tmp_path):  # type: ignore[no-untyped-def]
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()

    client = ClawHubClient()
    client.search_skills = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            _payload_compatible(),
            _payload_wrong_os(),
            _payload_missing_bin(),
            _payload_no_metadata(),
        ]
    )
    client.list_skills = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "results": [
                _payload_compatible(),
                _payload_wrong_os(),
            ]
        }
    )
    client.fetch_skill_detail = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "slug": "mac-only",
            "latestVersion": {"manifest": {"openclaw": {"os": ["darwin"]}}},
        }
    )
    client.aclose = AsyncMock()  # type: ignore[method-assign]

    installer = SkillInstaller(client, paths=paths)
    router = Router()
    register_skills_methods(router, client=client, installer=installer, paths=paths)
    return router


async def test_search_filters_incompatible_by_default(setup) -> None:  # type: ignore[no-untyped-def]
    router = setup
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "skills.search", "params": {"query": "x"}}
    )
    assert resp.error is None
    slugs = [r["slug"] for r in resp.result["results"]]
    # mac-only and missing-bin filtered out; compatible + no-metadata pass.
    assert "weather-cli" in slugs
    assert "unknown" in slugs
    assert "mac-only" not in slugs
    assert "needs-foo" not in slugs
    assert resp.result["filtered_count"] == 2
    # Surviving entries carry compat annotation.
    weather = next(r for r in resp.result["results"] if r["slug"] == "weather-cli")
    assert weather["compat"]["installable"] is True


async def test_search_include_incompatible_returns_all(setup) -> None:  # type: ignore[no-untyped-def]
    router = setup
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "skills.search",
            "params": {"query": "x", "include_incompatible": True},
        }
    )
    assert resp.error is None
    slugs = sorted(r["slug"] for r in resp.result["results"])
    assert slugs == ["mac-only", "needs-foo", "unknown", "weather-cli"]
    assert resp.result["filtered_count"] == 0
    mac = next(r for r in resp.result["results"] if r["slug"] == "mac-only")
    assert mac["compat"]["installable"] is False
    assert mac["compat"]["unsupported_os"] is True


async def test_list_remote_filters_incompatible_by_default(setup) -> None:  # type: ignore[no-untyped-def]
    router = setup
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "skills.list_remote", "params": {}}
    )
    assert resp.error is None
    slugs = [r["slug"] for r in resp.result["results"]]
    assert slugs == ["weather-cli"]
    assert resp.result["filtered_count"] == 1


async def test_detail_always_returns_compat_even_when_incompatible(setup) -> None:  # type: ignore[no-untyped-def]
    """Detail-by-slug must NEVER hide the result — the user asked
    by name. Surface the reason instead so they understand."""
    router = setup
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "skills.detail",
            "params": {"slug": "mac-only"},
        }
    )
    assert resp.error is None
    assert resp.result["detail"]["slug"] == "mac-only"
    assert resp.result["compat"]["installable"] is False
    # Reason should mention the platform mismatch.
    reasons = " ".join(resp.result["compat"]["reasons"])
    assert "darwin" in reasons
