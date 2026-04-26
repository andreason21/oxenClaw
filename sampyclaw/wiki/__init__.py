"""memory-wiki — durable knowledge vault.

Mirrors openclaw `extensions/memory-wiki/`. A persistent markdown vault
where the agent records *durable* knowledge (entities, concepts, sources,
syntheses, reports) with structured claim/evidence metadata.

Modules:
- `models`   — `WikiPageKind`, `WikiClaim`, `WikiEvidence`, `WikiPage`,
               `WikiVaultConfig`.
- `markdown` — frontmatter parse/serialize, slugify, related-link blocks.
- `vault`    — directory layout (`<vault>/<kind>/<slug>.md`), init.
- `ingest`   — append/upsert pages.
- `query`    — list/get/search by name/kind.
- `compile`  — deterministic vault index (`INDEX.md`).
- `palace`   — memory-palace prompt section (per-kind summaries for the
               system prompt).
- `lint`     — claim freshness + missing-evidence checks.
"""

from sampyclaw.wiki.compile import compile_wiki_index
from sampyclaw.wiki.ingest import upsert_wiki_page
from sampyclaw.wiki.lint import LintFinding, LintSeverity, lint_vault
from sampyclaw.wiki.markdown import (
    parse_wiki_markdown,
    render_wiki_markdown,
    slugify_wiki_segment,
)
from sampyclaw.wiki.models import (
    WIKI_PAGE_KINDS,
    WikiClaim,
    WikiEvidence,
    WikiPage,
    WikiPageKind,
    WikiVaultConfig,
)
from sampyclaw.wiki.palace import build_memory_palace_section
from sampyclaw.wiki.query import (
    get_wiki_page,
    list_wiki_pages,
    search_wiki_pages,
)
from sampyclaw.wiki.vault import (
    WikiVault,
    initialize_wiki_vault,
)

__all__ = [
    "WIKI_PAGE_KINDS",
    "LintFinding",
    "LintSeverity",
    "WikiClaim",
    "WikiEvidence",
    "WikiPage",
    "WikiPageKind",
    "WikiVault",
    "WikiVaultConfig",
    "build_memory_palace_section",
    "compile_wiki_index",
    "get_wiki_page",
    "initialize_wiki_vault",
    "lint_vault",
    "list_wiki_pages",
    "parse_wiki_markdown",
    "render_wiki_markdown",
    "search_wiki_pages",
    "slugify_wiki_segment",
    "upsert_wiki_page",
]
