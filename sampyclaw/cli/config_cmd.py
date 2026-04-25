"""`sampyclaw config` subcommands — show, validate."""

from __future__ import annotations

import json

import typer

from sampyclaw.config import ConfigError, default_paths, load_config

app = typer.Typer(help="Inspect and validate sampyclaw config.yaml.", no_args_is_help=True)


@app.command("show")
def show() -> None:
    """Print the resolved config as JSON."""
    cfg = load_config()
    typer.echo(json.dumps(cfg.model_dump(), indent=2, sort_keys=True))


@app.command("validate")
def validate(
    full: bool = typer.Option(
        True,
        "--full/--no-full",
        help="Also validate mcp.json, credentials, and env-var refs.",
    ),
) -> None:
    """Validate config.yaml and (by default) every other startup config.

    Exit code:
    - 0 if no errors
    - 1 if any errors (warnings alone don't fail)
    """
    if not full:
        try:
            load_config()
        except ConfigError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
        typer.echo(f"ok: {default_paths().config_file}")
        return

    from sampyclaw.config.preflight import run_preflight

    report = run_preflight()
    for finding in report.findings:
        stream_err = finding.severity == "error"
        typer.echo(finding.format(), err=stream_err)
    if report.errors:
        typer.echo(
            f"\npreflight failed: {len(report.errors)} error(s), "
            f"{len(report.warnings)} warning(s)",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(
        f"\npreflight ok: {len(report.warnings)} warning(s), no errors"
    )


@app.command("path")
def path_() -> None:
    """Print the config file path."""
    typer.echo(str(default_paths().config_file))
