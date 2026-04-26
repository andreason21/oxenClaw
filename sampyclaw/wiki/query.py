"""Query — list/get/search WikiPages.

Mirrors openclaw `memory-wiki/src/query.ts`. Search is intentionally
simple (substring match across name/aliases/body) — for semantic search
the operator should index the vault into the regular `MemoryStore` via
`SessionMemoryHook` or a custom indexer.
"""

from __future__ import annotations

from dataclasses import dataclass

from sampyclaw.wiki.models import WikiPage, WikiPageKind
from sampyclaw.wiki.vault import WikiVault


@dataclass(frozen=True)
class WikiSearchHit:
    page: WikiPage
    score: float
    matched_in: str  # "name" | "alias" | "tag" | "body" | "summary"


def get_wiki_page(vault: WikiVault, *, kind: WikiPageKind, slug: str) -> WikiPage | None:
    return vault.read_page(kind, slug)


def list_wiki_pages(vault: WikiVault, *, kind: WikiPageKind | None = None) -> list[WikiPage]:
    return list(vault.iter_pages(kind=kind))


def search_wiki_pages(
    vault: WikiVault,
    query: str,
    *,
    k: int = 10,
    kind: WikiPageKind | None = None,
) -> list[WikiSearchHit]:
    """Substring search across name/aliases/tags/summary/body.

    Score: 1.0 (name exact) > 0.8 (alias exact) > 0.6 (name substring) >
    0.4 (alias/tag substring) > 0.3 (summary substring) > 0.1 (body
    substring). Empty queries return [].
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    hits: list[WikiSearchHit] = []
    for page in vault.iter_pages(kind=kind):
        score = 0.0
        matched_in = ""
        name_l = page.name.lower()
        if name_l == q:
            score = 1.0
            matched_in = "name"
        elif any(a.lower() == q for a in page.aliases):
            score = 0.8
            matched_in = "alias"
        elif q in name_l:
            score = 0.6
            matched_in = "name"
        elif any(q in a.lower() for a in page.aliases):
            score = 0.4
            matched_in = "alias"
        elif any(q in t.lower() for t in page.tags):
            score = 0.4
            matched_in = "tag"
        elif page.summary and q in page.summary.lower():
            score = 0.3
            matched_in = "summary"
        elif page.body and q in page.body.lower():
            score = 0.1
            matched_in = "body"
        if score > 0:
            hits.append(WikiSearchHit(page=page, score=score, matched_in=matched_in))
    hits.sort(key=lambda h: (-h.score, h.page.name.lower()))
    return hits[:k]


__all__ = [
    "WikiSearchHit",
    "get_wiki_page",
    "list_wiki_pages",
    "search_wiki_pages",
]
