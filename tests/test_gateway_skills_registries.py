"""Gateway RPC: skills.registries + registry-aware install."""

from __future__ import annotations

import io
import zipfile
from unittest.mock import AsyncMock

import pytest

from sampyclaw.clawhub.client import ClawHubClient, sha256_integrity
from sampyclaw.clawhub.installer import SkillInstaller
from sampyclaw.clawhub.registries import (
    ClawHubRegistries,
    MultiRegistryClient,
    RegistryConfig,
)
from sampyclaw.config.paths import SampyclawPaths
from sampyclaw.gateway.router import Router
from sampyclaw.gateway.skills_methods import register_skills_methods


SAMPLE = """---
name: foo
description: x.
---
body
"""


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("foo/SKILL.md", SAMPLE)
    return buf.getvalue()


def _stub(client: ClawHubClient) -> ClawHubClient:
    archive = _zip()
    client.fetch_skill_detail = AsyncMock(  # type: ignore[method-assign]
        return_value={"latestVersion": {"version": "1.0.0"}}
    )
    client.download_skill_archive = AsyncMock(  # type: ignore[method-assign]
        return_value=(archive, sha256_integrity(archive))
    )
    client.search_skills = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"slug": "foo", "displayName": f"from {client.base_url}"}]
    )
    client.list_skills = AsyncMock(return_value={"results": []})  # type: ignore[method-assign]
    client.aclose = AsyncMock()  # type: ignore[method-assign]
    return client


@pytest.fixture()
def setup(tmp_path):  # type: ignore[no-untyped-def]
    paths = SampyclawPaths(home=tmp_path)
    paths.ensure_home()
    cfg = ClawHubRegistries(
        default="mirror",
        registries=[
            RegistryConfig(name="public", url="https://public", trust="official"),
            RegistryConfig(name="mirror", url="https://mirror", trust="mirror"),
        ],
    )
    multi = MultiRegistryClient(cfg)
    _stub(multi.get_client("public"))
    _stub(multi.get_client("mirror"))
    installer = SkillInstaller(multi, paths=paths)
    router = Router()
    register_skills_methods(router, client=multi, installer=installer, paths=paths)
    return router, multi, paths


async def test_registries_rpc(setup) -> None:  # type: ignore[no-untyped-def]
    router, _, _ = setup
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "skills.registries"}
    )
    assert resp.error is None
    names = [r["name"] for r in resp.result["registries"]]
    assert names == ["public", "mirror"]
    assert resp.result["default"] == "mirror"
    # Tokens are not exposed.
    assert all("token" not in r for r in resp.result["registries"])


async def test_search_routes_to_named_registry(setup) -> None:  # type: ignore[no-untyped-def]
    router, multi, _ = setup
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "skills.search",
            "params": {"query": "foo", "registry": "public"},
        }
    )
    assert resp.result["ok"] is True
    assert resp.result["registry"] == "public"
    assert "https://public" in resp.result["results"][0]["displayName"]


async def test_install_with_registry(setup) -> None:  # type: ignore[no-untyped-def]
    router, _, paths = setup
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "skills.install",
            "params": {"slug": "foo", "registry": "mirror"},
        }
    )
    assert resp.result["ok"] is True
    assert resp.result["registry"] == "mirror"
    assert resp.result["registry_url"] == "https://mirror"
    assert resp.result["trust"] == "mirror"


async def test_list_installed_includes_trust(setup) -> None:  # type: ignore[no-untyped-def]
    router, _, _ = setup
    await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "skills.install",
            "params": {"slug": "foo", "registry": "mirror"},
        }
    )
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 2, "method": "skills.list_installed"}
    )
    s = resp.result["skills"][0]
    assert s["registry_name"] == "mirror"
    assert s["trust"] == "mirror"


async def test_install_unknown_registry_returns_error(setup) -> None:  # type: ignore[no-untyped-def]
    router, _, _ = setup
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "skills.install",
            "params": {"slug": "foo", "registry": "ghost"},
        }
    )
    assert resp.result["ok"] is False
    assert "not configured" in resp.result["error"]
