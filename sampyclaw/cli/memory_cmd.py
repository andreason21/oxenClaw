"""`sampyclaw memory` — chunk-of-file corpus management."""

from __future__ import annotations

import asyncio
import json

import typer

from sampyclaw.config.paths import default_paths
from sampyclaw.memory import MemoryRetriever, OpenAIEmbeddings
from sampyclaw.memory.hybrid import HybridConfig
from sampyclaw.memory.mmr import MMRConfig
from sampyclaw.memory.temporal_decay import TemporalDecayConfig

app = typer.Typer(help="Manage long-term memory (chunk-of-file corpus).", no_args_is_help=True)


def _retriever(model: str | None, base_url: str | None) -> MemoryRetriever:
    embed_kwargs: dict[str, str] = {}
    if model:
        embed_kwargs["model"] = model
    if base_url:
        embed_kwargs["base_url"] = base_url
    return MemoryRetriever.for_root(default_paths(), OpenAIEmbeddings(**embed_kwargs))


@app.command("search")
def search(
    query: str = typer.Argument(..., help="Semantic query."),
    k: int = typer.Option(5, "-k"),
    source: str | None = typer.Option(None, "--source"),
    model: str | None = typer.Option(None, "--model"),
    base_url: str | None = typer.Option(None, "--base-url"),
    json_output: bool = typer.Option(False, "--json"),
    hybrid: bool = typer.Option(False, "--hybrid", help="Enable vector+BM25 merge."),
    vector_weight: float = typer.Option(0.7, "--vector-weight"),
    text_weight: float = typer.Option(0.3, "--text-weight"),
    mmr: bool = typer.Option(False, "--mmr", help="Enable MMR diversity."),
    mmr_lambda: float = typer.Option(0.7, "--mmr-lambda"),
    decay: bool = typer.Option(False, "--decay", help="Enable temporal decay."),
    half_life_days: float = typer.Option(30.0, "--half-life-days"),
) -> None:
    """Vector-search the memory corpus."""

    hybrid_cfg = (
        HybridConfig(
            enabled=True,
            vector_weight=vector_weight,
            text_weight=text_weight,
        )
        if hybrid
        else None
    )
    mmr_cfg = MMRConfig(enabled=True, lambda_=mmr_lambda) if mmr else None
    decay_cfg = TemporalDecayConfig(enabled=True, half_life_days=half_life_days) if decay else None

    async def _run() -> None:
        retriever = _retriever(model, base_url)
        try:
            hits = await retriever.search(
                query,
                k=k,
                source=source,
                hybrid=hybrid_cfg,
                mmr=mmr_cfg,
                temporal_decay=decay_cfg,
            )
        finally:
            await retriever.aclose()
        if json_output:
            typer.echo(
                json.dumps(
                    [
                        {
                            "citation": h.citation,
                            "score": h.score,
                            "distance": h.distance,
                            "path": h.chunk.path,
                            "start_line": h.chunk.start_line,
                            "end_line": h.chunk.end_line,
                            "text": h.chunk.text,
                        }
                        for h in hits
                    ],
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return
        if not hits:
            typer.echo("(no matches)")
            return
        for h in hits:
            preview = h.chunk.text.strip().splitlines()[0][:140]
            typer.echo(f"  {h.score:.2f}  {h.citation}  {preview}")

    asyncio.run(_run())


@app.command("sync")
def sync(
    model: str | None = typer.Option(None, "--model"),
    base_url: str | None = typer.Option(None, "--base-url"),
) -> None:
    """Re-index added/changed/deleted files in the corpus."""

    async def _run() -> None:
        retriever = _retriever(model, base_url)
        try:
            report = await retriever.sync()
        finally:
            await retriever.aclose()
        typer.echo(
            f"added={report.added} changed={report.changed} deleted={report.deleted} "
            f"embedded={report.chunks_embedded} cache_hits={report.cache_hits}"
        )

    asyncio.run(_run())


@app.command("list")
def list_(
    source: str | None = typer.Option(None, "--source"),
    model: str | None = typer.Option(None, "--model"),
    base_url: str | None = typer.Option(None, "--base-url"),
) -> None:
    """List files known to the memory store."""
    retriever = _retriever(model, base_url)
    try:
        files = retriever.store.list_files(source=source)
    finally:
        asyncio.run(retriever.aclose())
    if not files:
        typer.echo("(no files)")
        return
    for f in files:
        typer.echo(f"  {f.source:8s}  {f.chunk_count:4d}  {f.path}")


@app.command("stats")
def stats(
    model: str | None = typer.Option(None, "--model"),
    base_url: str | None = typer.Option(None, "--base-url"),
) -> None:
    """Report counts, dimensions, and meta."""
    retriever = _retriever(model, base_url)
    try:
        store = retriever.store
        typer.echo(f"path:        {store.path}")
        typer.echo(f"total files: {store.count_files()}")
        typer.echo(f"total chunks:{store.count_chunks()}")
        typer.echo(f"dimensions:  {store.dimensions or '(empty store)'}")
        meta = store.read_meta()
        if meta:
            typer.echo("meta:")
            for k, v in sorted(meta.items()):
                typer.echo(f"  {k} = {v}")
    finally:
        asyncio.run(retriever.aclose())


@app.command("get")
def get(
    path: str = typer.Argument(..., help="Relative path inside the corpus."),
    from_line: int = typer.Option(1, "--from", min=1),
    lines: int = typer.Option(120, "--lines", min=1),
    model: str | None = typer.Option(None, "--model"),
    base_url: str | None = typer.Option(None, "--base-url"),
) -> None:
    """Read a slice of a memory file."""
    retriever = _retriever(model, base_url)
    try:
        result = retriever.get(path, from_line=from_line, lines=lines)
    finally:
        asyncio.run(retriever.aclose())
    typer.echo(result.text)
    if result.truncated and result.next_from is not None:
        typer.echo(f"\n[More available: from={result.next_from}]")


@app.command("save")
def save(
    text: str = typer.Argument(..., help="The fact to remember."),
    tag: list[str] | None = typer.Option(None, "--tag"),
    model: str | None = typer.Option(None, "--model"),
    base_url: str | None = typer.Option(None, "--base-url"),
) -> None:
    """Append text to inbox.md and re-index."""

    async def _run() -> None:
        retriever = _retriever(model, base_url)
        try:
            report = await retriever.save(text, tags=tag or None)
            typer.echo(
                f"saved; added={report.added} changed={report.changed} "
                f"embedded={report.chunks_embedded} cache_hits={report.cache_hits}"
            )
        finally:
            await retriever.aclose()

    asyncio.run(_run())


@app.command("rebuild")
def rebuild(
    confirm: bool = typer.Option(False, "--yes", help="Skip confirmation."),
    model: str | None = typer.Option(None, "--model"),
    base_url: str | None = typer.Option(None, "--base-url"),
) -> None:
    """Wipe chunks/files and re-index from scratch."""
    if not confirm:
        typer.echo("pass --yes to confirm; aborting", err=True)
        raise typer.Exit(code=1)

    async def _run() -> None:
        retriever = _retriever(model, base_url)
        try:
            retriever.store.clear_all()
            report = await retriever.sync()
            typer.echo(f"rebuilt; added={report.added} embedded={report.chunks_embedded}")
        finally:
            await retriever.aclose()

    asyncio.run(_run())
