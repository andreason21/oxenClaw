"""RPC round-trip tests for wiki.* methods."""

from __future__ import annotations

from pathlib import Path

from oxenclaw.gateway.router import Router
from oxenclaw.gateway.wiki_methods import register_wiki_methods
from oxenclaw.wiki.models import WikiPage, WikiPageKind
from oxenclaw.wiki.store import WikiVaultStore


def _setup(tmp_path: Path) -> tuple[Router, WikiVaultStore]:
    vault = WikiVaultStore(tmp_path / "wiki")
    router = Router()
    register_wiki_methods(router, vault)
    return router, vault


async def _call(router: Router, method: str, params: dict | None = None) -> dict:
    raw: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        raw["params"] = params
    resp = await router.dispatch(raw)
    return resp.result


# ─── wiki.list ───────────────────────────────────────────────────────


async def test_list_empty_vault(tmp_path: Path) -> None:
    router, _ = _setup(tmp_path)
    result = await _call(router, "wiki.list")
    assert result["ok"] is True
    assert result["pages"] == []


async def test_list_returns_summaries(tmp_path: Path) -> None:
    router, vault = _setup(tmp_path)
    vault.create(WikiPage(kind=WikiPageKind.CONCEPT, name="Alpha", slug="alpha", body=""))
    vault.create(WikiPage(kind=WikiPageKind.ENTITY, name="Beta", slug="beta", body=""))
    result = await _call(router, "wiki.list")
    assert result["ok"] is True
    slugs = {p["slug"] for p in result["pages"]}
    assert slugs == {"alpha", "beta"}


async def test_list_filters_by_kind(tmp_path: Path) -> None:
    router, vault = _setup(tmp_path)
    vault.create(WikiPage(kind=WikiPageKind.CONCEPT, name="C", slug="c", body=""))
    vault.create(WikiPage(kind=WikiPageKind.ENTITY, name="E", slug="e", body=""))
    result = await _call(router, "wiki.list", {"kind": "concept"})
    assert result["ok"] is True
    assert all(p["kind"] == "concept" for p in result["pages"])


async def test_list_invalid_kind_returns_error(tmp_path: Path) -> None:
    router, _ = _setup(tmp_path)
    result = await _call(router, "wiki.list", {"kind": "unknown_kind"})
    assert result["ok"] is False
    assert "error" in result


# ─── wiki.get ────────────────────────────────────────────────────────


async def test_get_existing_page(tmp_path: Path) -> None:
    router, vault = _setup(tmp_path)
    vault.create(WikiPage(kind=WikiPageKind.CONCEPT, name="MyPage", slug="mypage", body="hello"))
    result = await _call(router, "wiki.get", {"slug": "mypage"})
    assert result["ok"] is True
    assert result["page"]["title"] == "MyPage"
    assert result["page"]["body"] == "hello"


async def test_get_missing_returns_error(tmp_path: Path) -> None:
    router, _ = _setup(tmp_path)
    result = await _call(router, "wiki.get", {"slug": "nope"})
    assert result["ok"] is False


# ─── wiki.create ─────────────────────────────────────────────────────


async def test_create_returns_page(tmp_path: Path) -> None:
    router, _ = _setup(tmp_path)
    result = await _call(
        router, "wiki.create", {"kind": "concept", "title": "New Page", "body": "content"}
    )
    assert result["ok"] is True
    assert result["page"]["slug"] == "new-page"
    assert result["page"]["kind"] == "concept"


async def test_create_with_claims(tmp_path: Path) -> None:
    router, _ = _setup(tmp_path)
    result = await _call(
        router,
        "wiki.create",
        {
            "kind": "entity",
            "title": "Alice",
            "claims": [{"text": "Alice is a researcher."}],
        },
    )
    assert result["ok"] is True
    assert len(result["page"]["claims"]) == 1
    assert result["page"]["claims"][0]["text"] == "Alice is a researcher."


async def test_create_duplicate_slug_returns_error(tmp_path: Path) -> None:
    router, _ = _setup(tmp_path)
    await _call(router, "wiki.create", {"kind": "concept", "title": "Dup"})
    result = await _call(router, "wiki.create", {"kind": "concept", "title": "Dup"})
    assert result["ok"] is False


async def test_create_invalid_kind_returns_error(tmp_path: Path) -> None:
    router, _ = _setup(tmp_path)
    result = await _call(router, "wiki.create", {"kind": "bogus", "title": "Oops"})
    assert result["ok"] is False


# ─── wiki.update ─────────────────────────────────────────────────────


async def test_update_title_and_body(tmp_path: Path) -> None:
    router, vault = _setup(tmp_path)
    vault.create(WikiPage(kind=WikiPageKind.CONCEPT, name="Old", slug="old", body="old body"))
    result = await _call(router, "wiki.update", {"slug": "old", "title": "New", "body": "new body"})
    assert result["ok"] is True
    assert result["page"]["title"] == "New"
    assert result["page"]["body"] == "new body"


async def test_update_missing_page_returns_error(tmp_path: Path) -> None:
    router, _ = _setup(tmp_path)
    result = await _call(router, "wiki.update", {"slug": "ghost", "title": "G"})
    assert result["ok"] is False


# ─── wiki.delete ─────────────────────────────────────────────────────


async def test_delete_existing(tmp_path: Path) -> None:
    router, vault = _setup(tmp_path)
    vault.create(WikiPage(kind=WikiPageKind.CONCEPT, name="Bye", slug="bye", body=""))
    result = await _call(router, "wiki.delete", {"slug": "bye"})
    assert result["ok"] is True
    assert result["deleted_slug"] == "bye"


async def test_delete_missing_returns_error(tmp_path: Path) -> None:
    router, _ = _setup(tmp_path)
    result = await _call(router, "wiki.delete", {"slug": "missing"})
    assert result["ok"] is False


# ─── wiki.search ─────────────────────────────────────────────────────


async def test_search_returns_hits(tmp_path: Path) -> None:
    router, vault = _setup(tmp_path)
    vault.create(WikiPage(kind=WikiPageKind.CONCEPT, name="Kraken", slug="kraken", body=""))
    result = await _call(router, "wiki.search", {"query": "kraken"})
    assert result["ok"] is True
    assert len(result["hits"]) >= 1


async def test_search_empty_query(tmp_path: Path) -> None:
    router, vault = _setup(tmp_path)
    vault.create(WikiPage(kind=WikiPageKind.CONCEPT, name="Anything", slug="anything", body=""))
    result = await _call(router, "wiki.search", {"query": ""})
    assert result["ok"] is True
    assert result["hits"] == []


# ─── wiki.add_claim ──────────────────────────────────────────────────


async def test_add_claim_success(tmp_path: Path) -> None:
    router, vault = _setup(tmp_path)
    vault.create(WikiPage(kind=WikiPageKind.CONCEPT, name="P", slug="p", body=""))
    result = await _call(router, "wiki.add_claim", {"slug": "p", "text": "A new fact."})
    assert result["ok"] is True
    assert result["claim"]["text"] == "A new fact."
    assert result["claim"]["claim_id"] is not None


async def test_add_claim_missing_page(tmp_path: Path) -> None:
    router, _ = _setup(tmp_path)
    result = await _call(router, "wiki.add_claim", {"slug": "ghost", "text": "fact"})
    assert result["ok"] is False


async def test_add_claim_persisted(tmp_path: Path) -> None:
    router, vault = _setup(tmp_path)
    vault.create(WikiPage(kind=WikiPageKind.CONCEPT, name="P", slug="p", body=""))
    await _call(router, "wiki.add_claim", {"slug": "p", "text": "Persistent fact."})
    page = vault.get("p")
    assert page is not None
    assert any(c.text == "Persistent fact." for c in page.claims)


# ─── wiki.verify ─────────────────────────────────────────────────────


async def test_verify_success(tmp_path: Path) -> None:
    from oxenclaw.wiki.claims import add_claim

    router, vault = _setup(tmp_path)
    page = WikiPage(kind=WikiPageKind.CONCEPT, name="P", slug="p", body="")
    page, claim = add_claim(page, "Verifiable.")
    vault.update("p", page)
    result = await _call(router, "wiki.verify", {"slug": "p", "claim_id": claim.claim_id})
    assert result["ok"] is True
    found = next((c for c in result["page"]["claims"] if c["claim_id"] == claim.claim_id), None)
    assert found is not None
    assert found["last_verified_at"] is not None


async def test_verify_missing_page(tmp_path: Path) -> None:
    router, _ = _setup(tmp_path)
    result = await _call(router, "wiki.verify", {"slug": "ghost", "claim_id": "abc"})
    assert result["ok"] is False


# ─── summary shape ───────────────────────────────────────────────────


async def test_list_summary_has_expected_fields(tmp_path: Path) -> None:
    router, vault = _setup(tmp_path)
    vault.create(WikiPage(kind=WikiPageKind.ENTITY, name="Zara", slug="zara", body=""))
    result = await _call(router, "wiki.list")
    summary = result["pages"][0]
    for key in ("slug", "title", "kind", "claim_count"):
        assert key in summary, f"missing key: {key}"
