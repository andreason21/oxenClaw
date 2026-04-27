"""Tests for wiki_search_tool / wiki_get_tool / wiki_save_tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from oxenclaw.wiki.models import WikiPage, WikiPageKind
from oxenclaw.wiki.store import WikiVaultStore
from oxenclaw.tools_pkg.wiki_tools import wiki_get_tool, wiki_save_tool, wiki_search_tool


def _vault(tmp_path: Path) -> WikiVaultStore:
    return WikiVaultStore(tmp_path / "wiki")


def _page(
    *,
    kind: WikiPageKind = WikiPageKind.CONCEPT,
    name: str = "Test",
    slug: str = "test",
    body: str = "",
) -> WikiPage:
    return WikiPage(kind=kind, name=name, slug=slug, body=body)


# ─── wiki_search_tool ────────────────────────────────────────────────


async def test_search_tool_returns_no_match_string(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    tool = wiki_search_tool(vault)
    result = await tool.execute({"query": "xyz"})
    assert "no wiki pages matched" in result


async def test_search_tool_returns_matching_page(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    vault.create(_page(name="Quantum Foam", slug="qfoam", body="planck"))
    tool = wiki_search_tool(vault)
    result = await tool.execute({"query": "quantum"})
    assert "Quantum Foam" in result


async def test_search_tool_name_is_wiki_search(tmp_path: Path) -> None:
    tool = wiki_search_tool(_vault(tmp_path))
    assert tool.name == "wiki_search"


async def test_search_tool_shows_claims_preview(tmp_path: Path) -> None:
    from oxenclaw.wiki.claims import add_claim

    vault = _vault(tmp_path)
    page = _page(name="Galaxy", slug="galaxy", body="")
    page, _ = add_claim(page, "Galaxy contains stars.")
    vault.update("galaxy", page)
    tool = wiki_search_tool(vault)
    result = await tool.execute({"query": "galaxy"})
    assert "Galaxy contains stars" in result


# ─── wiki_get_tool ───────────────────────────────────────────────────


async def test_get_tool_returns_not_found(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    tool = wiki_get_tool(vault)
    result = await tool.execute({"slug": "nope"})
    assert "not found" in result


async def test_get_tool_returns_page_content(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    vault.create(_page(name="Concept X", slug="cx", body="Body text here."))
    tool = wiki_get_tool(vault)
    result = await tool.execute({"slug": "cx"})
    assert "Concept X" in result
    assert "Body text here" in result


async def test_get_tool_name_is_wiki_get(tmp_path: Path) -> None:
    tool = wiki_get_tool(_vault(tmp_path))
    assert tool.name == "wiki_get"


async def test_get_tool_shows_claims(tmp_path: Path) -> None:
    from oxenclaw.wiki.claims import add_claim

    vault = _vault(tmp_path)
    page = _page(name="Entity A", slug="ea", body="")
    page, _ = add_claim(page, "Entity A is important.")
    vault.update("ea", page)
    tool = wiki_get_tool(vault)
    result = await tool.execute({"slug": "ea"})
    assert "Entity A is important." in result


# ─── wiki_save_tool ──────────────────────────────────────────────────


async def test_save_tool_creates_new_page(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    tool = wiki_save_tool(vault)
    result = await tool.execute({"kind": "concept", "title": "New Page", "body": "content"})
    assert "created" in result
    assert vault.get("new-page") is not None


async def test_save_tool_updates_existing_page(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    vault.create(_page(name="Existing", slug="existing", body="old"))
    tool = wiki_save_tool(vault)
    result = await tool.execute({"slug": "existing", "kind": "concept", "title": "Existing", "body": "new body"})
    assert "updated" in result
    page = vault.get("existing")
    assert page is not None
    assert page.body == "new body"


async def test_save_tool_appends_claims(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    tool = wiki_save_tool(vault)
    await tool.execute({"kind": "entity", "title": "Bob", "body": "researcher", "claims": ["Bob likes Python."]})
    page = vault.get("bob")
    assert page is not None
    assert any("Python" in c.text for c in page.claims)


async def test_save_tool_name_is_wiki_save(tmp_path: Path) -> None:
    tool = wiki_save_tool(_vault(tmp_path))
    assert tool.name == "wiki_save"


async def test_save_tool_invalid_kind_returns_error(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    tool = wiki_save_tool(vault)
    result = await tool.execute({"kind": "badkind", "title": "X", "body": ""})
    assert "error" in result.lower()


async def test_save_tool_no_duplicate_claims_on_update(tmp_path: Path) -> None:
    """Re-saving with the same claim text should not create a duplicate."""
    vault = _vault(tmp_path)
    tool = wiki_save_tool(vault)
    await tool.execute({"kind": "concept", "title": "Dupe", "body": "", "claims": ["Same claim."]})
    await tool.execute({"kind": "concept", "title": "Dupe", "body": "", "claims": ["Same claim."]})
    page = vault.get("dupe")
    assert page is not None
    claim_texts = [c.text for c in page.claims]
    assert claim_texts.count("Same claim.") == 1
