"""skills.* gateway RPC tests with a mocked ClawHubClient + fixture archive."""

from __future__ import annotations

import io
import zipfile
from unittest.mock import AsyncMock

import pytest

from oxenclaw.clawhub.client import ClawHubClient, sha256_integrity
from oxenclaw.clawhub.installer import SkillInstaller
from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.gateway.router import Router
from oxenclaw.gateway.skills_methods import register_skills_methods

SAMPLE_SKILL_MD = """---
name: foo
description: A test skill.
metadata:
  openclaw:
    emoji: 🧪
    requires:
      bins: [foo]
---

# body
"""


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("foo/SKILL.md", SAMPLE_SKILL_MD)
    return buf.getvalue()


@pytest.fixture()
def setup(tmp_path):  # type: ignore[no-untyped-def]
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()

    client = ClawHubClient()
    archive = _zip_bytes()
    client.search_skills = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"slug": "foo", "displayName": "Foo"}]
    )
    client.list_skills = AsyncMock(  # type: ignore[method-assign]
        return_value={"results": [{"slug": "foo"}, {"slug": "bar"}]}
    )
    client.fetch_skill_detail = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "skill": {"slug": "foo"},
            "latestVersion": {"version": "1.0.0"},
        }
    )
    client.download_skill_archive = AsyncMock(  # type: ignore[method-assign]
        return_value=(archive, sha256_integrity(archive))
    )
    client.aclose = AsyncMock()  # type: ignore[method-assign]

    installer = SkillInstaller(client, paths=paths)
    router = Router()
    register_skills_methods(router, client=client, installer=installer, paths=paths)
    return router, paths, client, installer


async def test_search_returns_results(setup) -> None:  # type: ignore[no-untyped-def]
    router, _, _, _ = setup
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "skills.search", "params": {"query": "foo"}}
    )
    assert resp.error is None
    assert resp.result["ok"] is True
    assert resp.result["results"][0]["slug"] == "foo"


async def test_list_remote(setup) -> None:  # type: ignore[no-untyped-def]
    router, _, _, _ = setup
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "skills.list_remote", "params": {}}
    )
    assert resp.result["ok"] is True
    assert any(s["slug"] == "bar" for s in resp.result["results"])


async def test_detail(setup) -> None:  # type: ignore[no-untyped-def]
    router, _, _, _ = setup
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "skills.detail", "params": {"slug": "foo"}}
    )
    assert resp.result["ok"] is True
    assert resp.result["detail"]["latestVersion"]["version"] == "1.0.0"


async def test_install_then_list_installed_then_uninstall(setup) -> None:  # type: ignore[no-untyped-def]
    router, paths, _, _ = setup

    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "skills.install", "params": {"slug": "foo"}}
    )
    assert resp.result["ok"] is True
    assert resp.result["version"] == "1.0.0"
    assert resp.result["manifest"]["name"] == "foo"
    assert resp.result["manifest"]["requires"]["bins"] == ["foo"]
    assert (paths.home / "skills" / "foo" / "SKILL.md").exists()

    listed = await router.dispatch({"jsonrpc": "2.0", "id": 2, "method": "skills.list_installed"})
    assert listed.result["ok"] is True
    assert listed.result["skills"][0]["slug"] == "foo"
    assert listed.result["skills"][0]["version"] == "1.0.0"

    removed = await router.dispatch(
        {"jsonrpc": "2.0", "id": 3, "method": "skills.uninstall", "params": {"slug": "foo"}}
    )
    assert removed.result == {"ok": True, "removed": True}

    listed2 = await router.dispatch({"jsonrpc": "2.0", "id": 4, "method": "skills.list_installed"})
    assert listed2.result["skills"] == []


async def test_install_existing_without_force_returns_error(setup) -> None:  # type: ignore[no-untyped-def]
    router, _, _, _ = setup
    await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "skills.install", "params": {"slug": "foo"}}
    )
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "skills.install", "params": {"slug": "foo"}}
    )
    assert resp.result["ok"] is False
    assert "already installed" in resp.result["error"]


async def test_update_after_install(setup) -> None:  # type: ignore[no-untyped-def]
    router, _, _, _ = setup
    await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "skills.install", "params": {"slug": "foo"}}
    )
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "skills.update", "params": {"slug": "foo"}}
    )
    assert resp.result["ok"] is True
    assert resp.result["version"] == "1.0.0"


async def test_install_unknown_slug_format(setup) -> None:  # type: ignore[no-untyped-def]
    router, _, _, _ = setup
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "skills.install",
            "params": {"slug": "bad slug!"},
        }
    )
    assert resp.result["ok"] is False
