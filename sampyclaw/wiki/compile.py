"""Compile — build a deterministic INDEX.md for the vault.

Mirrors openclaw `memory-wiki/src/compile.ts`. The index lists every
page grouped by kind, sorted alphabetically, so a diff between
compilations only changes the lines actually affected.
"""

from __future__ import annotations

from pathlib import Path

from sampyclaw.wiki.models import WIKI_PAGE_KINDS, WikiPage
from sampyclaw.wiki.vault import WikiVault

INDEX_FILENAME = "INDEX.md"
_INDEX_HEADER = "# Wiki Index\n\nAuto-generated. Edit `<kind>/<slug>.md` directly; re-run compile to regenerate.\n"


def _format_page_bullet(page: WikiPage) -> str:
    summary = f" — {page.summary}" if page.summary else ""
    return f"- [{page.name}]({page.relative_path}){summary}"


def compile_wiki_index(vault: WikiVault) -> Path:
    """Write `INDEX.md` at the vault root. Returns the path."""
    pages_by_kind: dict[str, list[WikiPage]] = {k: [] for k in WIKI_PAGE_KINDS}
    for page in vault.iter_pages():
        pages_by_kind.setdefault(page.kind.value, []).append(page)

    lines: list[str] = [_INDEX_HEADER]
    for kind in WIKI_PAGE_KINDS:
        bucket = sorted(pages_by_kind.get(kind, []), key=lambda p: p.name.lower())
        if not bucket:
            continue
        lines.append(f"\n## {kind.title()} ({len(bucket)})")
        lines.append("")
        for page in bucket:
            lines.append(_format_page_bullet(page))
    lines.append("")

    out_path = vault.root / INDEX_FILENAME
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


__all__ = ["INDEX_FILENAME", "compile_wiki_index"]
