"""Phase R2: memory-wiki — models, markdown, vault, ingest, query, compile,
palace, lint, and CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sampyclaw.cli.wiki_cmd import app as wiki_cli
from sampyclaw.wiki import (
    LintSeverity,
    WikiClaim,
    WikiEvidence,
    WikiPage,
    WikiPageKind,
    WikiVaultConfig,
    build_memory_palace_section,
    compile_wiki_index,
    get_wiki_page,
    initialize_wiki_vault,
    lint_vault,
    parse_wiki_markdown,
    render_wiki_markdown,
    search_wiki_pages,
    slugify_wiki_segment,
)
from sampyclaw.wiki.ingest import upsert_simple, upsert_wiki_page
from sampyclaw.wiki.lint import count_by_severity

# ─── slugify ─────────────────────────────────────────────────────────


def test_slugify_basic_normalisation() -> None:
    assert slugify_wiki_segment("Memory Palace") == "memory-palace"
    assert slugify_wiki_segment("foo_bar baz") == "foo-bar-baz"
    assert slugify_wiki_segment("###") == "untitled"


def test_slugify_collapses_runs_and_strips_edges() -> None:
    assert slugify_wiki_segment("---hello---world---") == "hello-world"


def test_slugify_truncates_with_hash_for_long_input() -> None:
    very_long = "x" * 1000
    out = slugify_wiki_segment(very_long)
    assert len(out.encode("utf-8")) <= 240
    assert "." in out


# ─── markdown round-trip ────────────────────────────────────────────


def _sample_page() -> WikiPage:
    return WikiPage(
        kind=WikiPageKind.CONCEPT,
        name="Quantum Foam",
        slug="quantum-foam",
        body="Detailed body content here.",
        aliases=("foam",),
        tags=("physics", "vacuum"),
        related=("planck-scale",),
        claims=(
            WikiClaim(
                text="Foam exists at the Planck scale.",
                evidence=(WikiEvidence(source_id="wheeler-1955", note="page 12"),),
                confidence=0.7,
            ),
        ),
        summary="Sub-Planck-scale vacuum fluctuations.",
    )


def test_render_then_parse_round_trips() -> None:
    page = _sample_page()
    rendered = render_wiki_markdown(page)
    assert rendered.startswith("---\n")
    assert "Quantum Foam" in rendered
    parsed = parse_wiki_markdown(rendered)
    assert parsed.kind is WikiPageKind.CONCEPT
    assert parsed.name == page.name
    assert parsed.aliases == page.aliases
    assert parsed.related == page.related
    assert parsed.claims and parsed.claims[0].evidence[0].source_id == "wheeler-1955"


def test_parse_rejects_missing_frontmatter() -> None:
    with pytest.raises(ValueError):
        parse_wiki_markdown("just a markdown body")


# ─── vault layout ───────────────────────────────────────────────────


def _vault(tmp_path: Path):  # type: ignore[no-untyped-def]
    return initialize_wiki_vault(WikiVaultConfig(path=tmp_path / "wiki"))


def test_vault_layout_creates_subdirs(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    assert (v.root / "concept").is_dir()
    assert (v.root / "synthesis").is_dir()
    assert (v.root / ".wiki").is_dir()


def test_write_and_read_roundtrip(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.write_page(_sample_page())
    p = v.read_page(WikiPageKind.CONCEPT, "quantum-foam")
    assert p is not None
    assert p.name == "Quantum Foam"


def test_iter_pages_filters_by_kind(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.write_page(_sample_page())
    v.write_page(
        WikiPage(
            kind=WikiPageKind.SOURCE,
            name="Wheeler 1955",
            slug="wheeler-1955",
            body="Citation",
        )
    )
    concepts = list(v.iter_pages(kind=WikiPageKind.CONCEPT))
    assert [p.slug for p in concepts] == ["quantum-foam"]
    sources = list(v.iter_pages(kind=WikiPageKind.SOURCE))
    assert [p.slug for p in sources] == ["wheeler-1955"]


def test_delete_page(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    v.write_page(_sample_page())
    assert v.delete_page(WikiPageKind.CONCEPT, "quantum-foam") is True
    assert v.delete_page(WikiPageKind.CONCEPT, "quantum-foam") is False


# ─── ingest / upsert ─────────────────────────────────────────────────


def test_upsert_creates_then_merges(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    p1 = upsert_wiki_page(v, _sample_page())
    assert p1.created_at == p1.updated_at

    # Re-upsert with new claim, expect merge.
    new_page = WikiPage(
        kind=WikiPageKind.CONCEPT,
        name="Quantum Foam",
        slug="quantum-foam",
        body="Updated body.",
        claims=(WikiClaim(text="New claim about foam."),),
    )
    p2 = upsert_wiki_page(v, new_page)
    claim_texts = {c.text for c in p2.claims}
    assert "Foam exists at the Planck scale." in claim_texts
    assert "New claim about foam." in claim_texts
    # created_at preserved, updated_at advanced.
    assert p2.created_at == p1.created_at
    assert p2.updated_at >= p1.updated_at


def test_upsert_simple_helper(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    p = upsert_simple(
        v,
        kind=WikiPageKind.ENTITY,
        name="Alice",
        body="ML researcher",
        summary="Researcher",
        tags=("person",),
    )
    assert p.slug == "alice"
    assert p.tags == ("person",)


# ─── query ──────────────────────────────────────────────────────────


def test_search_ranks_name_above_body(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    upsert_simple(v, kind=WikiPageKind.CONCEPT, name="Kraken", body="other text")
    upsert_simple(
        v,
        kind=WikiPageKind.CONCEPT,
        name="Octopus",
        body="kraken-related body content",
    )
    hits = search_wiki_pages(v, "kraken", k=5)
    assert hits[0].page.name == "Kraken"
    assert hits[0].matched_in == "name"
    assert hits[1].page.name == "Octopus"


def test_search_returns_empty_for_blank_query(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    upsert_simple(v, kind=WikiPageKind.CONCEPT, name="Anything")
    assert search_wiki_pages(v, "   ") == []


def test_get_returns_none_for_missing(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    assert get_wiki_page(v, kind=WikiPageKind.SOURCE, slug="nope") is None


# ─── compile / palace ───────────────────────────────────────────────


def test_compile_writes_index_with_kind_sections(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    upsert_simple(v, kind=WikiPageKind.CONCEPT, name="A")
    upsert_simple(v, kind=WikiPageKind.SOURCE, name="B")
    out = compile_wiki_index(v)
    text = out.read_text(encoding="utf-8")
    assert "## Concept" in text
    assert "## Source" in text
    assert "A" in text and "B" in text


def test_palace_lists_kinds_in_order(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    upsert_simple(v, kind=WikiPageKind.CONCEPT, name="Concept-One")
    upsert_simple(v, kind=WikiPageKind.SYNTHESIS, name="Synth-One")
    upsert_simple(v, kind=WikiPageKind.ENTITY, name="Entity-One")
    block = build_memory_palace_section(v)
    assert block.startswith("[Memory palace")
    # Synthesis should appear before Entity, before Concept.
    assert block.index("Syntheses") < block.index("Entities") < block.index("Concepts")


def test_palace_caps_with_more_indicator(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    for i in range(20):
        upsert_simple(v, kind=WikiPageKind.CONCEPT, name=f"Concept-{i:02}")
    block = build_memory_palace_section(v, max_per_kind=5)
    assert "+15 more" in block


def test_palace_empty_returns_empty_string(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    assert build_memory_palace_section(v) == ""


# ─── lint ───────────────────────────────────────────────────────────


def test_lint_clean_vault_returns_empty(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    upsert_simple(
        v,
        kind=WikiPageKind.CONCEPT,
        name="Clean",
        summary="Has a summary",
    )
    # Add a claim so the entity/concept "no claims" info doesn't fire.
    p = v.read_page(WikiPageKind.CONCEPT, "clean")
    assert p is not None
    upsert_wiki_page(
        v,
        WikiPage(
            kind=WikiPageKind.CONCEPT,
            name=p.name,
            slug=p.slug,
            summary=p.summary,
            body=p.body,
            claims=(
                WikiClaim(
                    text="Statement with evidence",
                    evidence=(WikiEvidence(path="https://example.org"),),
                ),
            ),
        ),
    )
    findings = lint_vault(v)
    counts = count_by_severity(findings)
    assert counts["error"] == 0
    assert counts["warning"] == 0


def test_lint_flags_orphan_related_link(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    upsert_wiki_page(
        v,
        WikiPage(
            kind=WikiPageKind.CONCEPT,
            name="Has Orphan",
            slug="has-orphan",
            related=("nonexistent",),
        ),
    )
    findings = lint_vault(v)
    msgs = [f.message for f in findings if f.severity is LintSeverity.ERROR]
    assert any("nonexistent" in m for m in msgs)


def test_lint_flags_missing_evidence(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    upsert_wiki_page(
        v,
        WikiPage(
            kind=WikiPageKind.CONCEPT,
            name="No Evidence",
            slug="no-evidence",
            claims=(WikiClaim(text="Bare claim"),),
        ),
    )
    findings = lint_vault(v)
    warnings = [f for f in findings if f.severity is LintSeverity.WARNING]
    assert any("no evidence" in f.message for f in warnings)


def test_lint_flags_dangling_source_id(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    upsert_wiki_page(
        v,
        WikiPage(
            kind=WikiPageKind.CONCEPT,
            name="Dangling",
            slug="dangling",
            claims=(
                WikiClaim(
                    text="X",
                    evidence=(WikiEvidence(source_id="missing-source"),),
                ),
            ),
        ),
    )
    findings = lint_vault(v)
    errors = [f for f in findings if f.severity is LintSeverity.ERROR]
    assert any("missing-source" in f.message for f in errors)


# ─── CLI smoke ──────────────────────────────────────────────────────


def test_cli_full_lifecycle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SAMPYCLAW_HOME", str(tmp_path))
    runner = CliRunner()

    res = runner.invoke(wiki_cli, ["init"])
    assert res.exit_code == 0
    assert "vault initialised" in res.stdout

    res = runner.invoke(wiki_cli, ["add", "concept", "Demo", "--summary", "demo summary"])
    assert res.exit_code == 0
    assert "wrote concept/demo" in res.stdout

    res = runner.invoke(wiki_cli, ["list", "--json"])
    assert res.exit_code == 0
    rows = json.loads(res.stdout)
    assert any(r["slug"] == "demo" for r in rows)

    res = runner.invoke(wiki_cli, ["search", "demo"])
    assert res.exit_code == 0
    assert "Demo" in res.stdout

    res = runner.invoke(wiki_cli, ["compile"])
    assert res.exit_code == 0
    assert "wrote " in res.stdout

    res = runner.invoke(wiki_cli, ["palace"])
    assert res.exit_code == 0
    assert "Memory palace" in res.stdout

    res = runner.invoke(wiki_cli, ["lint"])
    # `lint` may exit 1 if errors present — for our pristine page it should
    # still surface info-level findings (no claims) but no errors.
    assert res.exit_code == 0
