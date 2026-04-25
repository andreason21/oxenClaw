"""`sampyclaw session` CLI subcommands.

Mirrors the openclaw `commands-session*.ts` family. All operations work
against the local SQLite session store; no gateway connection needed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer

from sampyclaw.config.paths import default_paths
from sampyclaw.pi.lifecycle import (
    ForkOptions,
    ResetPolicy,
    archive_session,
    fork_session,
    reset_session,
    restore_archive,
)
from sampyclaw.pi.persistence import SQLiteSessionManager

app = typer.Typer(help="Inspect and manage agent sessions.")


def _store_path() -> Path:
    paths = default_paths()
    paths.ensure_home()
    return paths.home / "sessions.db"


def _archive_dir() -> Path:
    return default_paths().home / "archives"


def _open_store() -> SQLiteSessionManager:
    return SQLiteSessionManager(_store_path())


@app.command("list")
def list_sessions(
    agent_id: str | None = typer.Option(None, "--agent", help="Filter by agent id."),
    limit: int = typer.Option(50, help="Max rows."),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List sessions ordered by most recently updated."""

    async def _run() -> None:
        sm = _open_store()
        try:
            rows = await sm.list(agent_id=agent_id)
            rows = rows[:limit]
            if json_out:
                typer.echo(
                    json.dumps(
                        [
                            {
                                "id": r.id,
                                "title": r.title,
                                "agent_id": r.agent_id,
                                "model_id": r.model_id,
                                "messages": r.message_count,
                                "updated_at": r.updated_at,
                            }
                            for r in rows
                        ],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return
            if not rows:
                typer.echo("(no sessions)")
                return
            for r in rows:
                typer.echo(
                    f"{r.id[:8]}  {r.agent_id:<12}  msgs={r.message_count:<4} "
                    f"{r.title or '(untitled)'}"
                )
        finally:
            sm.close()

    asyncio.run(_run())


@app.command("show")
def show_session(
    session_id: str = typer.Argument(...),
    messages: bool = typer.Option(True, "--messages/--no-messages"),
) -> None:
    """Print a session's metadata and (optionally) full transcript."""

    async def _run() -> None:
        sm = _open_store()
        try:
            s = await sm.get(session_id)
            if s is None:
                typer.echo(f"no session {session_id!r}")
                raise typer.Exit(code=1)
            typer.echo(f"id:        {s.id}")
            typer.echo(f"agent:     {s.agent_id}")
            typer.echo(f"model:     {s.model_id}")
            typer.echo(f"title:     {s.title or '(untitled)'}")
            typer.echo(f"messages:  {len(s.messages)}")
            typer.echo(f"compactions: {len(s.compactions)}")
            if not messages:
                return
            typer.echo("---")
            for i, m in enumerate(s.messages):
                preview = ""
                content = getattr(m, "content", None)
                if isinstance(content, str):
                    preview = content[:120]
                elif isinstance(content, list) and content:
                    text_blocks = [b for b in content if getattr(b, "type", None) == "text"]
                    if text_blocks:
                        preview = text_blocks[0].text[:120]
                typer.echo(f"[{i:>3}] {m.role:<10} {preview}")
        finally:
            sm.close()

    asyncio.run(_run())


@app.command("reset")
def reset_cmd(
    session_id: str = typer.Argument(...),
    keep_system: bool = typer.Option(True, "--keep-system/--drop-system"),
    keep_last: int = typer.Option(0, "--keep-last", help="Keep last N user turns."),
    keep_compactions: bool = typer.Option(True, "--keep-compactions/--drop-compactions"),
) -> None:
    """Wipe a session's messages (preserving system + last N user turns optionally)."""

    async def _run() -> None:
        sm = _open_store()
        try:
            out = await reset_session(
                sm,
                session_id,
                policy=ResetPolicy(
                    full=True,
                    keep_system=keep_system,
                    keep_last_user_turns=keep_last,
                    keep_compactions=keep_compactions,
                ),
            )
            if out is None:
                typer.echo("no such session")
                raise typer.Exit(code=1)
            typer.echo(f"reset ok; remaining messages = {len(out.messages)}")
        finally:
            sm.close()

    asyncio.run(_run())


@app.command("fork")
def fork_cmd(
    session_id: str = typer.Argument(...),
    until_index: int | None = typer.Option(None, "--until-index"),
    title: str | None = typer.Option(None, "--title"),
) -> None:
    """Branch a session into a new id at `--until-index` (inclusive)."""

    async def _run() -> None:
        sm = _open_store()
        try:
            new = await fork_session(
                sm,
                session_id,
                options=ForkOptions(until_index=until_index, title=title),
            )
            if new is None:
                typer.echo("no such session")
                raise typer.Exit(code=1)
            typer.echo(f"forked → {new.id}  ({len(new.messages)} messages)")
        finally:
            sm.close()

    asyncio.run(_run())


@app.command("archive")
def archive_cmd(
    session_id: str = typer.Argument(...),
    keep: bool = typer.Option(False, "--keep", help="Don't delete after archive."),
) -> None:
    """Gzip a session to archives/ and (by default) delete it."""

    async def _run() -> None:
        sm = _open_store()
        try:
            res = await archive_session(
                sm, session_id, archive_dir=_archive_dir(), delete_after=not keep
            )
            if res is None:
                typer.echo("no such session")
                raise typer.Exit(code=1)
            typer.echo(f"archived → {res.archive_path}  ({res.bytes_written} bytes)")
        finally:
            sm.close()

    asyncio.run(_run())


@app.command("restore")
def restore_cmd(archive_path: Path = typer.Argument(...)) -> None:
    """Restore a previously-archived session into the store."""

    async def _run() -> None:
        sm = _open_store()
        try:
            out = await restore_archive(sm, archive_path)
            if out is None:
                typer.echo(f"no such archive: {archive_path}")
                raise typer.Exit(code=1)
            typer.echo(f"restored → {out.id}  ({len(out.messages)} messages)")
        finally:
            sm.close()

    asyncio.run(_run())


@app.command("delete")
def delete_cmd(
    session_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
) -> None:
    """Permanently delete a session."""
    if not yes:
        confirm = typer.confirm(f"Delete session {session_id}?", default=False)
        if not confirm:
            typer.echo("cancelled")
            raise typer.Exit(code=1)

    async def _run() -> None:
        sm = _open_store()
        try:
            ok = await sm.delete(session_id)
            typer.echo("deleted" if ok else "no such session")
            if not ok:
                raise typer.Exit(code=1)
        finally:
            sm.close()

    asyncio.run(_run())
