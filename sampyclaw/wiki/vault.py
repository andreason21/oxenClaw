"""WikiVault — directory layout + load/save primitives.

Mirrors openclaw `memory-wiki/src/vault.ts`. Layout:

    <vault>/
      INDEX.md              ← compiled index (deterministic)
      entity/<slug>.md
      concept/<slug>.md
      source/<slug>.md
      synthesis/<slug>.md
      report/<slug>.md
      .wiki/log.txt         ← append-only audit
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator

from sampyclaw.wiki.markdown import parse_wiki_markdown, render_wiki_markdown
from sampyclaw.wiki.models import (
    WIKI_PAGE_KINDS,
    WikiPage,
    WikiPageKind,
    WikiVaultConfig,
)


class WikiVault:
    """Filesystem view over a wiki vault."""

    def __init__(self, config: WikiVaultConfig) -> None:
        self._config = config
        self._root = Path(config.path).expanduser().resolve()

    @property
    def config(self) -> WikiVaultConfig:
        return self._config

    @property
    def root(self) -> Path:
        return self._root

    def page_path(self, kind: WikiPageKind, slug: str) -> Path:
        return self._root / kind.value / f"{slug}.md"

    # ─── load ───────────────────────────────────────────────────────

    def exists(self, kind: WikiPageKind, slug: str) -> bool:
        return self.page_path(kind, slug).exists()

    def read_page(self, kind: WikiPageKind, slug: str) -> WikiPage | None:
        path = self.page_path(kind, slug)
        if not path.exists():
            return None
        return parse_wiki_markdown(path.read_text(encoding="utf-8"))

    def iter_pages(
        self, *, kind: WikiPageKind | None = None
    ) -> Iterator[WikiPage]:
        kinds = [kind] if kind is not None else [
            WikiPageKind(k) for k in WIKI_PAGE_KINDS
        ]
        for k in kinds:
            sub = self._root / k.value
            if not sub.exists():
                continue
            for path in sorted(sub.glob("*.md")):
                try:
                    yield parse_wiki_markdown(path.read_text(encoding="utf-8"))
                except Exception:
                    # Skip malformed pages — `lint_vault` surfaces them.
                    continue

    # ─── save ───────────────────────────────────────────────────────

    def write_page(self, page: WikiPage) -> Path:
        path = self.page_path(page.kind, page.slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write — temp + rename.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(render_wiki_markdown(page), encoding="utf-8")
        tmp.replace(path)
        self._append_log(f"write {page.kind.value}/{page.slug}")
        return path

    def delete_page(self, kind: WikiPageKind, slug: str) -> bool:
        path = self.page_path(kind, slug)
        if not path.exists():
            return False
        path.unlink()
        self._append_log(f"delete {kind.value}/{slug}")
        return True

    # ─── log ────────────────────────────────────────────────────────

    def _log_path(self) -> Path:
        return self._root / ".wiki" / "log.txt"

    def _append_log(self, line: str) -> None:
        log = self._log_path()
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{int(time.time())}\t{line}\n")


def initialize_wiki_vault(config: WikiVaultConfig) -> WikiVault:
    """Create the directory layout (idempotent) and return a WikiVault."""
    vault = WikiVault(config)
    vault.root.mkdir(parents=True, exist_ok=True)
    for kind in WIKI_PAGE_KINDS:
        (vault.root / kind).mkdir(parents=True, exist_ok=True)
    (vault.root / ".wiki").mkdir(parents=True, exist_ok=True)
    return vault


__all__ = ["WikiVault", "initialize_wiki_vault"]
