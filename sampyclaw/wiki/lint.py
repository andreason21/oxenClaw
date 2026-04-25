"""Lint — vault consistency + claim-health checks.

Mirrors openclaw `memory-wiki/src/lint.ts` + `claim-health.ts`. Findings
are categorised by severity so callers can fail-loud (errors) vs
fail-quiet (info).

Checks:
- ERROR: page parse failure / orphan related-link target / source-id
  evidence pointing at a missing source page.
- WARNING: claim with zero evidence / claim marked contested without
  notes / page age > `stale_after_seconds`.
- INFO: page summary missing / page has no claims (entity/concept).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from sampyclaw.wiki.markdown import parse_wiki_markdown
from sampyclaw.wiki.models import WikiPage, WikiPageKind
from sampyclaw.wiki.vault import WikiVault


class LintSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class LintFinding:
    severity: LintSeverity
    page: str  # relative path
    message: str


def lint_vault(
    vault: WikiVault,
    *,
    stale_after_seconds: float = 90 * 24 * 60 * 60,  # 90 days
) -> list[LintFinding]:
    findings: list[LintFinding] = []

    # 1. Parse all pages, capturing error pages separately for orphan checks.
    pages_by_slug: dict[tuple[WikiPageKind, str], WikiPage] = {}
    all_slugs: set[str] = set()
    for path in sorted(vault.root.rglob("*.md")):
        if path.name == "INDEX.md":
            continue
        rel = str(path.relative_to(vault.root))
        try:
            page = parse_wiki_markdown(path.read_text(encoding="utf-8"))
        except Exception as exc:
            findings.append(
                LintFinding(LintSeverity.ERROR, rel, f"parse error: {exc}")
            )
            continue
        pages_by_slug[(page.kind, page.slug)] = page
        all_slugs.add(page.slug)

    now = time.time()
    source_slugs = {
        slug for (kind, slug) in pages_by_slug if kind is WikiPageKind.SOURCE
    }

    # 2. Per-page checks.
    for (kind, slug), page in pages_by_slug.items():
        rel = page.relative_path

        # Related-link orphans.
        for r in page.related:
            if r not in all_slugs:
                findings.append(
                    LintFinding(
                        LintSeverity.ERROR,
                        rel,
                        f"related link {r!r} does not resolve to a vault page",
                    )
                )

        # Claim health.
        for c in page.claims:
            if not c.evidence:
                findings.append(
                    LintFinding(
                        LintSeverity.WARNING,
                        rel,
                        f"claim {c.text[:60]!r} has no evidence",
                    )
                )
            for ev in c.evidence:
                if ev.source_id and ev.source_id not in source_slugs:
                    findings.append(
                        LintFinding(
                            LintSeverity.ERROR,
                            rel,
                            f"evidence source_id {ev.source_id!r} not found in vault",
                        )
                    )
            if c.contested and not any(ev.note for ev in c.evidence):
                findings.append(
                    LintFinding(
                        LintSeverity.WARNING,
                        rel,
                        f"contested claim has no notes: {c.text[:60]!r}",
                    )
                )

        # Staleness.
        if now - page.updated_at > stale_after_seconds:
            findings.append(
                LintFinding(
                    LintSeverity.WARNING,
                    rel,
                    f"page not updated in {(now - page.updated_at) / 86400:.0f} days",
                )
            )

        # Info-level shape suggestions.
        if not page.summary:
            findings.append(
                LintFinding(
                    LintSeverity.INFO,
                    rel,
                    "page has no summary (palace/index will skip a description)",
                )
            )
        if kind in (WikiPageKind.ENTITY, WikiPageKind.CONCEPT) and not page.claims:
            findings.append(
                LintFinding(
                    LintSeverity.INFO,
                    rel,
                    "entity/concept page has no claims",
                )
            )
    return findings


def count_by_severity(findings: list[LintFinding]) -> dict[str, int]:
    out = {s.value: 0 for s in LintSeverity}
    for f in findings:
        out[f.severity.value] += 1
    return out


__all__ = ["LintFinding", "LintSeverity", "count_by_severity", "lint_vault"]
