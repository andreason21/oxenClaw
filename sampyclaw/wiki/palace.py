"""Memory palace — wiki summary block for the agent's system prompt.

Mirrors openclaw `memory-wiki/src/memory-palace.ts` + `prompt-section.ts`.
The palace is a compact, kind-grouped index the agent sees on every turn
so it knows what's in the vault without consuming the full content.

Order: synthesis (highest signal) → entity → concept → source → report.
"""

from __future__ import annotations

from sampyclaw.wiki.models import WikiPage, WikiPageKind
from sampyclaw.wiki.vault import WikiVault


_PALACE_KIND_ORDER: tuple[WikiPageKind, ...] = (
    WikiPageKind.SYNTHESIS,
    WikiPageKind.ENTITY,
    WikiPageKind.CONCEPT,
    WikiPageKind.SOURCE,
    WikiPageKind.REPORT,
)
_PALACE_KIND_LABELS: dict[WikiPageKind, str] = {
    WikiPageKind.SYNTHESIS: "Syntheses",
    WikiPageKind.ENTITY: "Entities",
    WikiPageKind.CONCEPT: "Concepts",
    WikiPageKind.SOURCE: "Sources",
    WikiPageKind.REPORT: "Reports",
}


def build_memory_palace_section(
    vault: WikiVault,
    *,
    max_per_kind: int = 12,
    primary_kinds: tuple[WikiPageKind, ...] = (
        WikiPageKind.SYNTHESIS,
        WikiPageKind.ENTITY,
        WikiPageKind.CONCEPT,
    ),
) -> str:
    """Render the palace block. Empty vault → ''.

    Each kind shows up to `max_per_kind` pages by name. Pages outside
    `primary_kinds` are still included but capped tighter (half).
    """
    by_kind: dict[WikiPageKind, list[WikiPage]] = {k: [] for k in _PALACE_KIND_ORDER}
    for page in vault.iter_pages():
        by_kind.setdefault(page.kind, []).append(page)
    if not any(by_kind.values()):
        return ""

    lines: list[str] = ["[Memory palace — durable knowledge in the wiki]"]
    for kind in _PALACE_KIND_ORDER:
        bucket = sorted(by_kind.get(kind, []), key=lambda p: p.name.lower())
        if not bucket:
            continue
        cap = max_per_kind if kind in primary_kinds else max(1, max_per_kind // 2)
        sliced = bucket[:cap]
        lines.append(f"  {_PALACE_KIND_LABELS[kind]}:")
        for p in sliced:
            tail = f" — {p.summary}" if p.summary else ""
            lines.append(f"    - {p.name}{tail}")
        if len(bucket) > cap:
            lines.append(f"    - … +{len(bucket) - cap} more")
    return "\n".join(lines)


__all__ = ["build_memory_palace_section"]
