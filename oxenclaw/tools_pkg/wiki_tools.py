"""Wiki tools the agent can invoke during a turn.

Three tools:
  - ``wiki_search`` — substring search across the wiki vault.
  - ``wiki_get``    — return the full text + claims summary for a page.
  - ``wiki_save``   — create or update a wiki page (approval-gated).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.tools_pkg._arg_aliases import fold_aliases
from oxenclaw.wiki.models import WikiPageKind, parse_wiki_page_kind
from oxenclaw.wiki.store import SlugConflict, WikiVaultStore


class _SearchArgs(BaseModel):
    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def _absorb(cls, data: Any) -> Any:
        return fold_aliases(
            data,
            {"query": ("q", "search", "text", "prompt", "topic", "phrase")},
        )

    query: str = Field(..., description="Search phrase to look up in the wiki vault.")
    k: int = Field(10, description="Maximum number of pages to return.", ge=1, le=50)


class _GetArgs(BaseModel):
    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def _absorb(cls, data: Any) -> Any:
        return fold_aliases(data, {"slug": ("id", "page", "page_id", "name")})

    slug: str = Field(..., description="Slug of the wiki page to retrieve.")


class _SaveArgs(BaseModel):
    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def _absorb(cls, data: Any) -> Any:
        return fold_aliases(
            data,
            {
                "slug": ("id", "page", "page_id"),
                "title": ("name", "subject", "heading"),
                "body": ("content", "text", "markdown", "md"),
                "kind": ("type", "category"),
            },
        )

    slug: str | None = Field(
        None,
        description=(
            "Slug to update.  Omit (or leave null) to create a new page "
            "(slug is derived from title)."
        ),
    )
    kind: str = Field(
        ...,
        description="Page kind: entity, concept, source, synthesis, or report.",
    )
    title: str = Field(..., description="Human-readable page title.")
    body: str = Field("", description="Markdown body (after frontmatter).")
    claims: list[str] = Field(
        default_factory=list,
        description="Plain-text claim statements to attach to the page.",
    )


def wiki_search_tool(vault: WikiVaultStore) -> Tool:
    """Return a FunctionTool named ``wiki_search``."""

    async def _handler(args: _SearchArgs) -> str:
        pages = vault.search(args.query, k=args.k)
        if not pages:
            return "(no wiki pages matched)"
        lines: list[str] = []
        for p in pages:
            summary = f" — {p.summary}" if p.summary else ""
            lines.append(f"- [{p.kind.value}] **{p.name}** (slug: `{p.slug}`){summary}")
            if p.claims:
                for c in p.claims[:3]:
                    lines.append(f"  - {c.text[:120]}")
                if len(p.claims) > 3:
                    lines.append(f"  - … +{len(p.claims) - 3} more claims")
        return "\n".join(lines)

    return FunctionTool(
        name="wiki_search",
        description=(
            "Search the persistent wiki knowledge base by keyword. "
            "Use this when the user asks 'what do you know about X?' or "
            "references a past decision, entity, concept, or source that "
            "may be stored across sessions."
        ),
        input_model=_SearchArgs,
        handler=_handler,
    )


def wiki_get_tool(vault: WikiVaultStore) -> Tool:
    """Return a FunctionTool named ``wiki_get``."""

    async def _handler(args: _GetArgs) -> str:
        page = vault.get(args.slug)
        if page is None:
            return f"(wiki page not found: {args.slug!r})"
        parts: list[str] = [
            f"# {page.name}",
            f"kind: {page.kind.value}",
        ]
        if page.summary:
            parts.append(f"summary: {page.summary}")
        if page.body:
            parts.append("")
            parts.append(page.body)
        if page.claims:
            parts.append("")
            parts.append("## Claims")
            for c in page.claims:
                verified = ""
                if c.last_verified_at is not None:
                    import time as _t

                    age = (_t.time() - c.last_verified_at) / 86400
                    verified = f" (verified {age:.0f}d ago)"
                conf = f" [{c.confidence:.0%}]" if c.confidence is not None else ""
                parts.append(f"- {c.text}{conf}{verified}")
        return "\n".join(parts)

    return FunctionTool(
        name="wiki_get",
        description=(
            "Retrieve a wiki page by slug. Returns the full markdown body "
            "plus a claims summary. Use after wiki_search to read the "
            "complete content of a matched page."
        ),
        input_model=_GetArgs,
        handler=_handler,
    )


def wiki_save_tool(
    vault: WikiVaultStore,
    *,
    approval_manager: Any | None = None,  # ApprovalManager | None
) -> Tool:
    """Return a FunctionTool named ``wiki_save``.

    When ``approval_manager`` is provided the tool is approval-gated
    (mutating action against persistent knowledge).
    """
    from oxenclaw.wiki.claims import add_claim as _add_claim
    from oxenclaw.wiki.markdown import slugify_wiki_segment
    from oxenclaw.wiki.models import WikiPage

    async def _handler(args: _SaveArgs) -> str:
        # Validate kind.
        try:
            kind: WikiPageKind = parse_wiki_page_kind(args.kind)
        except ValueError as exc:
            return f"error: {exc}"

        slug = (args.slug or "").strip() or slugify_wiki_segment(args.title)
        existing = vault.get(slug)

        if existing is not None:
            # Update path.
            from dataclasses import replace as _replace

            updated = _replace(
                existing,
                name=args.title,
                body=args.body,
                kind=kind,
            )
            # Append new claims that aren't already present by text.
            existing_texts = {c.text for c in updated.claims}
            for text in args.claims:
                if text not in existing_texts:
                    updated, _ = _add_claim(updated, text)
                    existing_texts.add(text)
            vault.update(slug, updated)
            return f"updated wiki page: {slug!r} ({len(updated.claims)} claims)"
        else:
            # Create path.
            page = WikiPage(
                kind=kind,
                name=args.title,
                slug=slug,
                body=args.body,
            )
            for text in args.claims:
                page, _ = _add_claim(page, text)
            try:
                saved = vault.create(page)
            except SlugConflict:
                # Race: another call created it while we ran; fall through
                # to an update so we don't silently drop the data.
                saved_existing = vault.get(slug)
                if saved_existing is None:
                    return f"error: slug conflict but page is missing: {slug!r}"
                vault.update(slug, page)
                return f"updated wiki page (race): {slug!r}"
            return f"created wiki page: {saved.slug!r} ({len(saved.claims)} claims)"

    tool = FunctionTool(
        name="wiki_save",
        description=(
            "Create or update a wiki page in the persistent knowledge base. "
            "Always approval-gated when a knowledge-base mutation requires "
            "user confirmation. Use to record authoritative decisions, entity "
            "facts, or concepts the user wants preserved across sessions."
        ),
        input_model=_SaveArgs,
        handler=_handler,
    )

    if approval_manager is not None:
        try:
            from oxenclaw.approvals.tool_wrap import require_approval

            return require_approval(tool, approval_manager)
        except Exception:
            # Approval wrapping is best-effort; if unavailable, return
            # the tool unwrapped rather than failing boot.
            pass

    return tool


__all__ = ["wiki_get_tool", "wiki_save_tool", "wiki_search_tool"]
