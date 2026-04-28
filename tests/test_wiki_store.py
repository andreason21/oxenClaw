"""Tests for WikiVaultStore — create / update / get / list / delete / atomic-write."""

from __future__ import annotations

from pathlib import Path

import pytest

from oxenclaw.wiki.models import WikiPage, WikiPageKind
from oxenclaw.wiki.store import SlugConflict, WikiVaultStore


def _vault(tmp_path: Path) -> WikiVaultStore:
    return WikiVaultStore(tmp_path / "wiki")


def _page(
    *,
    kind: WikiPageKind = WikiPageKind.CONCEPT,
    name: str = "Test Page",
    body: str = "body content",
) -> WikiPage:
    return WikiPage(kind=kind, name=name, slug="", body=body)


# ─── init ────────────────────────────────────────────────────────────


def test_init_creates_root_dir(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    assert not root.exists()
    WikiVaultStore(root)
    assert root.is_dir()


def test_init_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    WikiVaultStore(root)
    WikiVaultStore(root)  # second call must not raise
    assert root.is_dir()


# ─── create ─────────────────────────────────────────────────────────


def test_create_returns_page_with_derived_slug(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    p = v.create(_page(name="Hello World"))
    assert p.slug == "hello-world"
    assert (v.root / "hello-world.md").exists()


def test_create_sets_timestamps(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    p = v.create(_page())
    assert p.created_at > 0
    assert p.updated_at > 0
    assert abs(p.created_at - p.updated_at) < 1.0


def test_create_refuses_duplicate_slug(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.create(_page(name="Alpha"))
    with pytest.raises(SlugConflict):
        v.create(_page(name="Alpha"))


def test_create_uses_explicit_slug(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    page = WikiPage(kind=WikiPageKind.ENTITY, name="Bob", slug="my-custom-slug", body="")
    saved = v.create(page)
    assert saved.slug == "my-custom-slug"
    assert (v.root / "my-custom-slug.md").exists()


def test_create_atomic_write(tmp_path: Path) -> None:
    """No .tmp file should be left after a successful write."""
    v = _vault(tmp_path)
    v.create(_page(name="Atom"))
    tmp_files = list(v.root.glob("*.tmp"))
    assert tmp_files == []


# ─── get ─────────────────────────────────────────────────────────────


def test_get_returns_none_for_missing(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    assert v.get("nonexistent") is None


def test_get_round_trips(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    created = v.create(_page(name="Round Trip", body="hello"))
    fetched = v.get(created.slug)
    assert fetched is not None
    assert fetched.name == "Round Trip"
    assert fetched.body == "hello"
    assert fetched.kind == WikiPageKind.CONCEPT


# ─── update ──────────────────────────────────────────────────────────


def test_update_changes_body_and_title(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    created = v.create(_page(name="Old Name", body="old body"))
    from dataclasses import replace

    updated_page = replace(created, name="New Name", body="new body")
    saved = v.update(created.slug, updated_page)
    assert saved.name == "New Name"
    assert saved.body == "new body"
    assert saved.slug == created.slug
    assert saved.created_at == created.created_at


def test_update_advances_updated_at(tmp_path: Path) -> None:
    import time

    v = _vault(tmp_path)
    created = v.create(_page(name="Tick"))
    time.sleep(0.01)
    from dataclasses import replace

    saved = v.update(created.slug, replace(created, body="new"))
    assert saved.updated_at >= created.updated_at


def test_update_creates_if_missing(tmp_path: Path) -> None:
    """update() should write the file even if slug doesn't exist yet."""
    v = _vault(tmp_path)
    page = WikiPage(kind=WikiPageKind.ENTITY, name="Ghost", slug="ghost", body="boo")
    saved = v.update("ghost", page)
    assert saved.slug == "ghost"
    assert v.get("ghost") is not None


# ─── delete ──────────────────────────────────────────────────────────


def test_delete_existing_returns_true(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    p = v.create(_page(name="Delete Me"))
    assert v.delete(p.slug) is True
    assert v.get(p.slug) is None


def test_delete_missing_returns_false(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    assert v.delete("nope") is False


# ─── list ─────────────────────────────────────────────────────────────


def test_list_returns_all_pages(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.create(WikiPage(kind=WikiPageKind.CONCEPT, name="C", slug="c", body=""))
    v.create(WikiPage(kind=WikiPageKind.ENTITY, name="E", slug="e", body=""))
    pages = v.list()
    slugs = {p.slug for p in pages}
    assert {"c", "e"} == slugs


def test_list_filters_by_kind(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.create(WikiPage(kind=WikiPageKind.CONCEPT, name="C", slug="c", body=""))
    v.create(WikiPage(kind=WikiPageKind.ENTITY, name="E", slug="e", body=""))
    concepts = v.list(kind=WikiPageKind.CONCEPT)
    assert len(concepts) == 1
    assert concepts[0].slug == "c"


def test_list_empty_vault(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    assert v.list() == []


# ─── search ──────────────────────────────────────────────────────────


def test_search_matches_title(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.create(WikiPage(kind=WikiPageKind.CONCEPT, name="Quantum Foam", slug="qfoam", body=""))
    v.create(WikiPage(kind=WikiPageKind.CONCEPT, name="Octopus", slug="octo", body="unrelated"))
    hits = v.search("quantum")
    assert len(hits) == 1
    assert hits[0].slug == "qfoam"


def test_search_matches_body(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.create(
        WikiPage(kind=WikiPageKind.CONCEPT, name="Alpha", slug="alpha", body="contains kraken text")
    )
    v.create(WikiPage(kind=WikiPageKind.CONCEPT, name="Beta", slug="beta", body="unrelated"))
    hits = v.search("kraken")
    assert any(p.slug == "alpha" for p in hits)


def test_search_empty_query_returns_empty(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.create(WikiPage(kind=WikiPageKind.CONCEPT, name="Something", slug="s", body=""))
    assert v.search("") == []
    assert v.search("   ") == []


def test_search_respects_k(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    for i in range(15):
        v.create(WikiPage(kind=WikiPageKind.CONCEPT, name=f"thing {i}", slug=f"thing-{i}", body=""))
    hits = v.search("thing", k=5)
    assert len(hits) <= 5


def test_search_title_ranked_higher_than_body(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.create(WikiPage(kind=WikiPageKind.CONCEPT, name="Kraken", slug="kraken", body=""))
    v.create(WikiPage(kind=WikiPageKind.CONCEPT, name="Octopus", slug="octo", body="kraken facts"))
    hits = v.search("kraken")
    assert hits[0].slug == "kraken"  # name match scores higher
