"""WikiVault — flat file-backed store for the runtime RPC layer.

Mirrors openclaw `memory-wiki` but uses a *flat* layout:

    <root>/          (e.g. ~/.oxenclaw/wiki/)
      <slug>.md      ← frontmatter (YAML) + body

Every page is stored at ``<root>/<slug>.md`` regardless of kind.  This
makes the vault easier to navigate with a text editor and keeps the RPC
layer simple — slugs are the only key.

The class delegates serialisation to the existing
``oxenclaw.wiki.markdown`` helpers so the on-disk format is identical
to the directory-layout vault (kind/slug sub-paths are not used here).

Slug derivation rule
--------------------
If the caller supplies a non-empty ``page.slug`` it is used verbatim
(after stripping whitespace).  Otherwise the slug is derived by calling
``slugify_wiki_segment(page.name)`` which lowercases, replaces
non-alphanumeric runs with ``-``, collapses runs, strips leading/
trailing dashes, and appends an 8-hex hash suffix when truncation was
required to stay within 240 UTF-8 bytes.

Atomic writes
-------------
All writes go through ``tmp + os.replace`` so a crash between the
``write`` and the ``rename`` never leaves a half-written page on disk.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path

from oxenclaw.wiki.markdown import (
    parse_wiki_markdown,
    render_wiki_markdown,
    slugify_wiki_segment,
)
from oxenclaw.wiki.models import WikiPage, WikiPageKind


class WikiStoreError(Exception):
    """Base class for WikiVault errors."""


class SlugConflict(WikiStoreError):
    """Raised by ``create`` when a page with the given slug already exists."""


class WikiVaultStore:
    """Flat file-backed vault under a single root directory.

    Public surface
    --------------
    __init__(root)        — ensures dir exists.
    create(page)          — atomic write; refuses to overwrite an existing slug.
    update(slug, page)    — atomic write; slug must exist.
    get(slug)             — parse frontmatter + body; None if missing.
    delete(slug)          — remove; returns True iff the file existed.
    list(kind?)           — cheap walk; parses each frontmatter.
    search(query, k?)     — substring match against title + body.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    # ─── helpers ────────────────────────────────────────────────────

    def _page_path(self, slug: str) -> Path:
        return self._root / f"{slug}.md"

    def _derive_slug(self, page: WikiPage) -> str:
        raw = (page.slug or "").strip()
        return raw if raw else slugify_wiki_segment(page.name)

    def _atomic_write(self, path: Path, content: str) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)

    # ─── CRUD ───────────────────────────────────────────────────────

    def create(self, page: WikiPage) -> WikiPage:
        """Write ``page`` to the vault.

        Raises ``SlugConflict`` when a page with the derived slug
        already exists.  Timestamps are set to ``now`` on creation.
        """
        slug = self._derive_slug(page)
        path = self._page_path(slug)
        if path.exists():
            raise SlugConflict(f"page already exists: {slug!r}")
        now = time.time()
        saved = replace(page, slug=slug, created_at=now, updated_at=now)
        self._atomic_write(path, render_wiki_markdown(saved))
        return saved

    def update(self, slug: str, page: WikiPage) -> WikiPage:
        """Overwrite ``slug`` with the data in ``page``.

        ``page.slug`` and ``page.created_at`` are preserved from the
        *existing* page so callers don't need to track them.
        """
        path = self._page_path(slug)
        existing = self.get(slug)
        created_at = existing.created_at if existing is not None else time.time()
        saved = replace(page, slug=slug, created_at=created_at, updated_at=time.time())
        self._atomic_write(path, render_wiki_markdown(saved))
        return saved

    def get(self, slug: str) -> WikiPage | None:
        """Parse and return the page for ``slug``, or ``None`` if absent."""
        path = self._page_path(slug)
        if not path.exists():
            return None
        try:
            return parse_wiki_markdown(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def delete(self, slug: str) -> bool:
        """Delete the page for ``slug``.  Returns ``True`` iff it existed."""
        path = self._page_path(slug)
        if not path.exists():
            return False
        path.unlink()
        return True

    def list(self, *, kind: WikiPageKind | None = None) -> list[WikiPage]:
        """Return all pages, optionally filtered by kind.

        Malformed pages are silently skipped (``lint_vault`` surfaces them).
        """
        pages: list[WikiPage] = []
        for path in sorted(self._root.glob("*.md")):
            try:
                page = parse_wiki_markdown(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if kind is not None and page.kind != kind:
                continue
            pages.append(page)
        return pages

    def search(self, query: str, *, k: int = 10) -> list[WikiPage]:
        """Naive substring search across title + body.

        Returns up to ``k`` pages sorted by descending hit-count.

        .. note::
            TODO: wire to memory's vector store in a future session for
            semantic search.
        """
        q = (query or "").strip().lower()
        if not q:
            return []
        scored: list[tuple[int, WikiPage]] = []
        for page in self.list():
            name_hits = page.name.lower().count(q)
            body_hits = (page.body or "").lower().count(q)
            score = name_hits * 3 + body_hits  # name hits weighted higher
            if score > 0:
                scored.append((score, page))
        scored.sort(key=lambda x: -x[0])
        return [p for _, p in scored[:k]]


__all__ = ["SlugConflict", "WikiStoreError", "WikiVaultStore"]
