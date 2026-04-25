"""`sampyclaw wiki` CLI subcommands.

Mirrors openclaw `memory-wiki/src/cli.ts`. The wiki lives at
`~/.sampyclaw/wiki/main` by default; override with `--vault PATH`.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from sampyclaw.config.paths import default_paths
from sampyclaw.wiki import (
    LintSeverity,
    WikiPage,
    WikiPageKind,
    WikiVaultConfig,
    build_memory_palace_section,
    compile_wiki_index,
    initialize_wiki_vault,
    lint_vault,
    list_wiki_pages,
    parse_wiki_markdown,
    search_wiki_pages,
)
from sampyclaw.wiki.ingest import upsert_simple
from sampyclaw.wiki.lint import count_by_severity

app = typer.Typer(help="Browse and curate the durable knowledge wiki.")


def _default_vault_path() -> Path:
    return default_paths().home / "wiki" / "main"


def _open_vault(vault: Path | None):  # type: ignore[no-untyped-def]
    cfg = WikiVaultConfig(path=vault or _default_vault_path())
    return initialize_wiki_vault(cfg)


@app.command("init")
def init_cmd(
    vault: Path | None = typer.Option(None, "--vault"),
) -> None:
    """Create the vault directory layout (idempotent)."""
    v = _open_vault(vault)
    typer.echo(f"vault initialised at {v.root}")


@app.command("list")
def list_cmd(
    vault: Path | None = typer.Option(None, "--vault"),
    kind: str | None = typer.Option(None, "--kind"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """List vault pages, optionally filtered by kind."""
    v = _open_vault(vault)
    parsed_kind = WikiPageKind(kind) if kind else None
    pages = list_wiki_pages(v, kind=parsed_kind)
    if json_out:
        typer.echo(
            json.dumps(
                [
                    {"kind": p.kind.value, "slug": p.slug, "name": p.name,
                     "summary": p.summary}
                    for p in pages
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    for p in pages:
        suffix = f" — {p.summary}" if p.summary else ""
        typer.echo(f"{p.kind.value}/{p.slug}\t{p.name}{suffix}")


@app.command("show")
def show_cmd(
    relative_path: str = typer.Argument(..., help="<kind>/<slug>"),
    vault: Path | None = typer.Option(None, "--vault"),
) -> None:
    v = _open_vault(vault)
    file_path = v.root / relative_path
    if not file_path.exists() and not file_path.suffix:
        file_path = file_path.with_suffix(".md")
    if not file_path.exists():
        typer.echo(f"no such page: {relative_path}")
        raise typer.Exit(code=1)
    typer.echo(file_path.read_text(encoding="utf-8"))


@app.command("add")
def add_cmd(
    kind: str = typer.Argument(..., help="entity / concept / source / synthesis / report"),
    name: str = typer.Argument(...),
    summary: str | None = typer.Option(None, "--summary"),
    body: str = typer.Option("", "--body"),
    vault: Path | None = typer.Option(None, "--vault"),
) -> None:
    v = _open_vault(vault)
    page = upsert_simple(
        v, kind=WikiPageKind(kind), name=name, body=body, summary=summary
    )
    typer.echo(f"wrote {page.relative_path}")


@app.command("search")
def search_cmd(
    query: str = typer.Argument(...),
    vault: Path | None = typer.Option(None, "--vault"),
    k: int = typer.Option(10),
    kind: str | None = typer.Option(None, "--kind"),
) -> None:
    v = _open_vault(vault)
    parsed_kind = WikiPageKind(kind) if kind else None
    hits = search_wiki_pages(v, query, k=k, kind=parsed_kind)
    if not hits:
        typer.echo("(no hits)")
        return
    for h in hits:
        typer.echo(
            f"[{h.score:.2f} {h.matched_in:<8}] "
            f"{h.page.kind.value}/{h.page.slug}\t{h.page.name}"
        )


@app.command("compile")
def compile_cmd(vault: Path | None = typer.Option(None, "--vault")) -> None:
    v = _open_vault(vault)
    out = compile_wiki_index(v)
    typer.echo(f"wrote {out}")


@app.command("palace")
def palace_cmd(vault: Path | None = typer.Option(None, "--vault")) -> None:
    v = _open_vault(vault)
    block = build_memory_palace_section(v)
    typer.echo(block or "(empty palace)")


@app.command("lint")
def lint_cmd(vault: Path | None = typer.Option(None, "--vault")) -> None:
    v = _open_vault(vault)
    findings = lint_vault(v)
    if not findings:
        typer.echo("(clean)")
        return
    counts = count_by_severity(findings)
    typer.echo(f"{counts}")
    for f in findings:
        typer.echo(f"[{f.severity.value:<7}] {f.page}: {f.message}")
    if counts.get(LintSeverity.ERROR.value, 0) > 0:
        raise typer.Exit(code=1)
