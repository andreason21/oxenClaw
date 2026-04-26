"""Ingest — append/upsert WikiPage into the vault.

Mirrors openclaw `memory-wiki/src/ingest.ts`. Upsert is the common
operation: if the slug exists, merge claims (dedup by `text`) and update
`updated_at`. New pages get fresh timestamps.
"""

from __future__ import annotations

import time
from dataclasses import replace

from oxenclaw.wiki.markdown import slugify_wiki_segment
from oxenclaw.wiki.models import WikiClaim, WikiPage, WikiPageKind
from oxenclaw.wiki.vault import WikiVault


def _merge_claims(
    existing: tuple[WikiClaim, ...], incoming: tuple[WikiClaim, ...]
) -> tuple[WikiClaim, ...]:
    """De-duplicate by claim.text — incoming wins on conflict."""
    by_text: dict[str, WikiClaim] = {c.text: c for c in existing}
    for c in incoming:
        by_text[c.text] = c
    return tuple(by_text.values())


def upsert_wiki_page(vault: WikiVault, page: WikiPage) -> WikiPage:
    """Write `page` into `vault`, merging with an existing slug if present.

    The returned WikiPage reflects the persisted state (with updated
    timestamps and merged claims).
    """
    slug = page.slug or slugify_wiki_segment(page.name)
    if slug != page.slug:
        page = replace(page, slug=slug)

    existing = vault.read_page(page.kind, slug)
    if existing is None:
        now = time.time()
        merged = replace(page, created_at=now, updated_at=now)
    else:
        merged = replace(
            page,
            created_at=existing.created_at,
            updated_at=time.time(),
            claims=_merge_claims(existing.claims, page.claims),
            related=tuple(dict.fromkeys((*existing.related, *page.related))),
            aliases=tuple(dict.fromkeys((*existing.aliases, *page.aliases))),
            tags=tuple(dict.fromkeys((*existing.tags, *page.tags))),
        )
    vault.write_page(merged)
    return merged


def upsert_simple(
    vault: WikiVault,
    *,
    kind: WikiPageKind,
    name: str,
    body: str = "",
    summary: str | None = None,
    tags: tuple[str, ...] = (),
) -> WikiPage:
    """Quick path for the common `kind + name + body` ingest."""
    return upsert_wiki_page(
        vault,
        WikiPage(
            kind=kind,
            name=name,
            slug=slugify_wiki_segment(name),
            body=body,
            summary=summary,
            tags=tags,
        ),
    )


__all__ = ["upsert_simple", "upsert_wiki_page"]
