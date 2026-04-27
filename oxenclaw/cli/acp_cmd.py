"""`oxenclaw acp` — run oxenclaw as an ACP agent over stdio.

Thin typer wrapper around `oxenclaw.acp.server.main`. Lets clients
spawn `oxenclaw acp` instead of the more verbose
`python -m oxenclaw.acp.server`.
"""

from __future__ import annotations

import typer

app = typer.Typer(
    help="Run oxenclaw as an ACP (Agent Client Protocol) agent over stdio.",
    no_args_is_help=False,
)


@app.callback(invoke_without_command=True)
def serve(
    ctx: typer.Context,
    backend: str = typer.Option(
        "fake",
        "--backend",
        help=(
            "Runtime backend id (default: 'fake' — in-memory echo). "
            "Real backends register at import time; pass their id here."
        ),
    ),
) -> None:
    """Start the stdio ACP agent. Reads NDJSON JSON-RPC from stdin,
    writes responses + session/update notifications to stdout."""
    if ctx.invoked_subcommand is not None:
        return
    from oxenclaw.acp.server import main as server_main

    raise typer.Exit(server_main(["--backend", backend]))
