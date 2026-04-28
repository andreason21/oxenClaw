"""wiki.* JSON-RPC methods bound to a WikiVaultStore.

RPC surface
-----------
wiki.list(kind?)                  → list of summary dicts
wiki.get(slug)                    → full page dict
wiki.create({kind, title, body?, claims?}) → created page dict
wiki.update({slug, title?, body?, claims?}) → updated page dict
wiki.delete({slug})               → {ok, deleted_slug}
wiki.search({query, k?})          → list of {score, page} dicts
wiki.add_claim({slug, text, evidence?, confidence?}) → new claim dict
wiki.verify({slug, claim_id})     → updated page dict
"""

from __future__ import annotations

import time
from dataclasses import replace as _replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from oxenclaw.gateway.router import Router
from oxenclaw.wiki.claims import add_claim as _claims_add_claim
from oxenclaw.wiki.claims import verify_claim as _claims_verify_claim
from oxenclaw.wiki.models import (
    WikiClaim,
    WikiEvidence,
    WikiPage,
    WikiPageKind,
    parse_wiki_page_kind,
)
from oxenclaw.wiki.store import SlugConflict, WikiVaultStore

# ─── param models ───────────────────────────────────────────────────


class _ListParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str | None = None


class _GetParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str


class _EvidenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str | None = None
    path: str | None = None
    lines: str | None = None
    note: str | None = None
    weight: float | None = None


class _ClaimIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    evidence: list[_EvidenceIn] = Field(default_factory=list)
    confidence: float | None = None
    contested: bool = False


class _CreateParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    title: str
    body: str = ""
    claims: list[_ClaimIn] = Field(default_factory=list)


class _UpdateParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str
    title: str | None = None
    body: str | None = None
    claims: list[_ClaimIn] | None = None


class _DeleteParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str


class _SearchParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    k: int = 10


class _AddClaimParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str
    text: str
    evidence: list[_EvidenceIn] = Field(default_factory=list)
    confidence: float | None = None


class _VerifyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str
    claim_id: str


# ─── serialisers ────────────────────────────────────────────────────


def _serialise_claim(c: WikiClaim) -> dict[str, Any]:
    out: dict[str, Any] = {"text": c.text, "contested": c.contested}
    if c.claim_id is not None:
        out["claim_id"] = c.claim_id
    if c.confidence is not None:
        out["confidence"] = c.confidence
    if c.asserted_at is not None:
        out["asserted_at"] = c.asserted_at
    if c.last_verified_at is not None:
        out["last_verified_at"] = c.last_verified_at
    out["evidence"] = [
        {
            k: v
            for k, v in {
                "source_id": e.source_id,
                "path": e.path,
                "lines": e.lines,
                "note": e.note,
                "weight": e.weight,
            }.items()
            if v is not None
        }
        for e in c.evidence
    ]
    return out


def _serialise_page(page: WikiPage) -> dict[str, Any]:
    return {
        "slug": page.slug,
        "kind": page.kind.value,
        "title": page.name,
        "body": page.body,
        "summary": page.summary,
        "aliases": list(page.aliases),
        "tags": list(page.tags),
        "related": list(page.related),
        "claims": [_serialise_claim(c) for c in page.claims],
        "created_at": page.created_at,
        "updated_at": page.updated_at,
    }


def _serialise_summary(page: WikiPage) -> dict[str, Any]:
    return {
        "slug": page.slug,
        "kind": page.kind.value,
        "title": page.name,
        "last_verified_at": max(
            (c.last_verified_at for c in page.claims if c.last_verified_at is not None),
            default=None,
        ),
        "claim_count": len(page.claims),
    }


def _build_claims(raw: list[_ClaimIn]) -> tuple[WikiClaim, ...]:
    result: list[WikiClaim] = []
    for r in raw:
        ev = tuple(
            WikiEvidence(
                source_id=e.source_id,
                path=e.path,
                lines=e.lines,
                note=e.note,
                weight=e.weight,
            )
            for e in r.evidence
        )
        result.append(
            WikiClaim(
                text=r.text,
                evidence=ev,
                confidence=r.confidence,
                contested=r.contested,
                asserted_at=time.time(),
            )
        )
    return tuple(result)


# ─── registration ───────────────────────────────────────────────────


def register_wiki_methods(router: Router, vault: WikiVaultStore) -> None:
    """Bind all wiki.* RPC methods to ``router`` using ``vault``."""

    @router.method("wiki.list", _ListParams)
    async def _list(p: _ListParams) -> dict[str, Any]:
        kind: WikiPageKind | None = None
        if p.kind:
            try:
                kind = parse_wiki_page_kind(p.kind)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
        pages = vault.list(kind=kind)
        return {"ok": True, "pages": [_serialise_summary(pg) for pg in pages]}

    @router.method("wiki.get", _GetParams)
    async def _get(p: _GetParams) -> dict[str, Any]:
        page = vault.get(p.slug)
        if page is None:
            return {"ok": False, "error": f"page not found: {p.slug!r}"}
        return {"ok": True, "page": _serialise_page(page)}

    @router.method("wiki.create", _CreateParams)
    async def _create(p: _CreateParams) -> dict[str, Any]:
        try:
            kind = parse_wiki_page_kind(p.kind)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        from oxenclaw.wiki.markdown import slugify_wiki_segment

        page = WikiPage(
            kind=kind,
            name=p.title,
            slug=slugify_wiki_segment(p.title),
            body=p.body,
            claims=_build_claims(p.claims),
        )
        try:
            saved = vault.create(page)
        except SlugConflict as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "page": _serialise_page(saved)}

    @router.method("wiki.update", _UpdateParams)
    async def _update(p: _UpdateParams) -> dict[str, Any]:
        existing = vault.get(p.slug)
        if existing is None:
            return {"ok": False, "error": f"page not found: {p.slug!r}"}
        updated = _replace(
            existing,
            name=p.title if p.title is not None else existing.name,
            body=p.body if p.body is not None else existing.body,
            claims=(_build_claims(p.claims) if p.claims is not None else existing.claims),
        )
        saved = vault.update(p.slug, updated)
        return {"ok": True, "page": _serialise_page(saved)}

    @router.method("wiki.delete", _DeleteParams)
    async def _delete(p: _DeleteParams) -> dict[str, Any]:
        deleted = vault.delete(p.slug)
        if not deleted:
            return {"ok": False, "error": f"page not found: {p.slug!r}"}
        return {"ok": True, "deleted_slug": p.slug}

    @router.method("wiki.search", _SearchParams)
    async def _search(p: _SearchParams) -> dict[str, Any]:
        q = p.query.lower()
        pages = vault.search(p.query, k=p.k)
        hits = [
            {
                "score": pg.name.lower().count(q) * 3 + (pg.body or "").lower().count(q),
                "page": _serialise_summary(pg),
            }
            for pg in pages
        ]
        return {"ok": True, "hits": hits}

    @router.method("wiki.add_claim", _AddClaimParams)
    async def _add_claim(p: _AddClaimParams) -> dict[str, Any]:
        page = vault.get(p.slug)
        if page is None:
            return {"ok": False, "error": f"page not found: {p.slug!r}"}
        ev = [
            WikiEvidence(
                source_id=e.source_id,
                path=e.path,
                lines=e.lines,
                note=e.note,
                weight=e.weight,
            )
            for e in p.evidence
        ]
        new_page, new_claim = _claims_add_claim(page, p.text, ev, p.confidence)
        vault.update(p.slug, new_page)
        return {"ok": True, "claim": _serialise_claim(new_claim)}

    @router.method("wiki.verify", _VerifyParams)
    async def _verify(p: _VerifyParams) -> dict[str, Any]:
        page = vault.get(p.slug)
        if page is None:
            return {"ok": False, "error": f"page not found: {p.slug!r}"}
        updated = _claims_verify_claim(page, p.claim_id)
        saved = vault.update(p.slug, updated)
        return {"ok": True, "page": _serialise_page(saved)}


__all__ = ["register_wiki_methods"]
