"""Wiki data models — page kinds, claims, evidence, vault config.

Mirrors openclaw `memory-wiki/src/markdown.ts` types. Frozen dataclasses
keep round-trips deterministic so file content is stable across re-saves
(important for diff-based version control on the vault).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal

WIKI_PAGE_KINDS: tuple[str, ...] = (
    "entity",  # a person, org, project, file
    "concept",  # an idea / definition / pattern
    "source",  # a citation: paper / URL / book
    "synthesis",  # a derived claim that combines sources/concepts
    "report",  # a one-shot output document (status reports, post-mortems)
)


class WikiPageKind(StrEnum):
    ENTITY = "entity"
    CONCEPT = "concept"
    SOURCE = "source"
    SYNTHESIS = "synthesis"
    REPORT = "report"


_VALID_KINDS = {k.value for k in WikiPageKind}


def parse_wiki_page_kind(value: str) -> WikiPageKind:
    if value not in _VALID_KINDS:
        raise ValueError(f"unknown WikiPageKind {value!r}; allowed: {sorted(_VALID_KINDS)}")
    return WikiPageKind(value)


@dataclass(frozen=True)
class WikiEvidence:
    """A single citation backing a claim."""

    source_id: str | None = None  # slug of a `source` page
    path: str | None = None  # external URL or local file
    lines: str | None = None  # e.g. "L42-L58"
    note: str | None = None  # short justification
    weight: float | None = None  # 0.0..1.0 confidence
    updated_at: float | None = None


@dataclass(frozen=True)
class WikiClaim:
    """A statement the page asserts, with backing evidence + freshness."""

    text: str
    evidence: tuple[WikiEvidence, ...] = ()
    contested: bool = False
    confidence: float | None = None
    asserted_at: float | None = None
    last_verified_at: float | None = None


@dataclass
class WikiPage:
    """A single vault page."""

    kind: WikiPageKind
    name: str  # human-readable title
    slug: str  # filesystem-safe id
    body: str = ""  # markdown body (after frontmatter)
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    related: tuple[str, ...] = ()  # slugs of related pages
    claims: tuple[WikiClaim, ...] = ()
    summary: str | None = None
    provenance_mode: str | None = None  # isolated/bridge/unsafe-local
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def relative_path(self) -> str:
        return f"{self.kind.value}/{self.slug}.md"


@dataclass(frozen=True)
class WikiVaultConfig:
    """Vault-level options."""

    path: Path
    render_mode: Literal["native", "obsidian"] = "native"
    obsidian_vault_name: str | None = None


__all__ = [
    "WIKI_PAGE_KINDS",
    "WikiClaim",
    "WikiEvidence",
    "WikiPage",
    "WikiPageKind",
    "WikiVaultConfig",
    "parse_wiki_page_kind",
]
