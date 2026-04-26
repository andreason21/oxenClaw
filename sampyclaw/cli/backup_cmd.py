"""`sampyclaw backup` subcommands — create, restore, verify, list."""

from __future__ import annotations

from pathlib import Path

import typer

from sampyclaw.backup import (
    create_backup,
    list_backups,
    restore_backup,
    verify_backup,
)
from sampyclaw.config.paths import default_paths

app = typer.Typer(
    help="Backup and restore the sampyClaw home directory.",
    no_args_is_help=True,
)


@app.command("create")
def create(
    output: Path | None = typer.Argument(
        None,
        help="Output file or directory. Default: cwd/sampyclaw-backup-<ts>.tar.gz.",
    ),
) -> None:
    """Create a snapshot of `~/.sampyclaw/`."""
    result = create_backup(output)
    typer.echo(
        f"backup ok: {result.archive_path}\n"
        f"  files: {result.file_count}\n"
        f"  uncompressed: {result.bytes_uncompressed:,} bytes\n"
        f"  archive size: {result.archive_path.stat().st_size:,} bytes"
    )


@app.command("verify")
def verify(archive: Path) -> None:
    """Verify integrity of a backup archive (every file SHA256 matches)."""
    try:
        manifest = verify_backup(archive)
    except Exception as exc:
        typer.echo(f"verify failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"verify ok: {archive}\n"
        f"  format version: {manifest.version}\n"
        f"  created: {manifest.created_at}\n"
        f"  files: {len(manifest.files)}"
    )


@app.command("restore")
def restore(
    archive: Path,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be restored without changes."
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Permit restoring into a non-empty home dir (files in the backup replace existing ones).",
    ),
) -> None:
    """Restore a backup archive into `~/.sampyclaw/`."""
    try:
        result = restore_backup(archive, dry_run=dry_run, overwrite=overwrite)
    except Exception as exc:
        typer.echo(f"restore failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if dry_run:
        typer.echo(
            f"dry-run ok: would restore {len(result.restored_files)} file(s) into {result.target}"
        )
        return
    typer.echo(f"restore ok: {len(result.restored_files)} file(s) restored into {result.target}")
    if result.skipped_files:
        typer.echo(f"  skipped: {len(result.skipped_files)} file(s)")


@app.command("list")
def list_(
    directory: Path = typer.Argument(
        Path.cwd(),
        help="Directory to scan for backup archives.",
    ),
) -> None:
    """List backup archives in a directory, newest first."""
    candidates = list_backups(directory)
    if not candidates:
        typer.echo(f"(no backups in {directory})")
        return
    for path in candidates:
        size = path.stat().st_size
        typer.echo(f"{size:>10}  {path.name}")


@app.command("home")
def home() -> None:
    """Print the sampyClaw home directory that backups capture."""
    typer.echo(str(default_paths().home))
