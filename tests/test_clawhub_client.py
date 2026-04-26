"""ClawHubClient HTTP tests using aiohttp's test_utils — no live network."""

from __future__ import annotations

import pytest
from aiohttp import web

from oxenclaw.clawhub.client import ClawHubClient, ClawHubError, sha256_integrity


def test_sha256_integrity_format() -> None:
    out = sha256_integrity(b"hello")
    assert out.startswith("sha256-")
    assert len(out) == len("sha256-") + 64


@pytest.fixture()
async def fake_clawhub(aiohttp_server):  # type: ignore[no-untyped-def]
    routes = web.RouteTableDef()

    @routes.get("/api/v1/search")
    async def _search(request: web.Request) -> web.Response:
        q = request.query.get("q")
        return web.json_response({"results": [{"slug": "match", "displayName": q}]})

    @routes.get("/api/v1/skills")
    async def _list(_: web.Request) -> web.Response:
        return web.json_response({"results": [{"slug": "a"}, {"slug": "b"}]})

    @routes.get("/api/v1/skills/{slug}")
    async def _detail(request: web.Request) -> web.Response:
        slug = request.match_info["slug"]
        return web.json_response({"skill": {"slug": slug}, "latestVersion": {"version": "9.9.9"}})

    @routes.get("/api/v1/download")
    async def _download(_: web.Request) -> web.Response:
        return web.Response(body=b"DUMMY-ARCHIVE-BYTES")

    @routes.get("/api/v1/error")
    async def _err(_: web.Request) -> web.Response:
        return web.Response(status=503, text="upstream busy")

    app = web.Application()
    app.add_routes(routes)
    server = await aiohttp_server(app)
    return server


@pytest.fixture()
async def client(fake_clawhub) -> ClawHubClient:  # type: ignore[no-untyped-def]
    base_url = str(fake_clawhub.make_url("")).rstrip("/")
    c = ClawHubClient(base_url=base_url, token=None)
    yield c
    await c.aclose()


async def test_search_skills(client: ClawHubClient) -> None:
    out = await client.search_skills("hello", limit=5)
    assert out == [{"slug": "match", "displayName": "hello"}]


async def test_list_skills(client: ClawHubClient) -> None:
    out = await client.list_skills(limit=10)
    assert out["results"] == [{"slug": "a"}, {"slug": "b"}]


async def test_fetch_skill_detail(client: ClawHubClient) -> None:
    out = await client.fetch_skill_detail("foo")
    assert out["skill"]["slug"] == "foo"
    assert out["latestVersion"]["version"] == "9.9.9"


async def test_download_skill_archive(client: ClawHubClient) -> None:
    body, integrity = await client.download_skill_archive("foo", version="1.0")
    assert body == b"DUMMY-ARCHIVE-BYTES"
    assert integrity == sha256_integrity(body)


async def test_error_response_raises(client: ClawHubClient) -> None:
    # Use a low-level _get_json with a known-failing path.
    with pytest.raises(ClawHubError) as excinfo:
        await client._get_json("/api/v1/error")
    assert excinfo.value.status == 503


async def test_token_is_sent_as_bearer(aiohttp_server) -> None:  # type: ignore[no-untyped-def]
    captured: dict = {}  # type: ignore[type-arg]

    async def echo_token(request: web.Request) -> web.Response:
        captured["auth"] = request.headers.get("Authorization")
        return web.json_response({"results": []})

    app = web.Application()
    app.router.add_get("/api/v1/search", echo_token)
    server = await aiohttp_server(app)

    base_url = str(server.make_url("")).rstrip("/")
    c = ClawHubClient(base_url=base_url, token="sk-abc")
    try:
        await c.search_skills("foo")
    finally:
        await c.aclose()
    assert captured.get("auth") == "Bearer sk-abc"
