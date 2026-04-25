"""Top-level `sampyclaw` CLI.

Port of openclaw `src/cli/*`. Uses typer for type-driven argument parsing.
Subcommands live in sibling modules.
"""

from __future__ import annotations

import typer

from sampyclaw.cli import (
    backup_cmd,
    config_cmd,
    gateway_cmd,
    memory_cmd,
    message_cmd,
    sessions_cmd,
    skills_cmd,
    wiki_cmd,
)

app = typer.Typer(help="sampyclaw — Python port of openclaw.", no_args_is_help=True)
app.add_typer(config_cmd.app, name="config", help="Inspect and edit config.yaml.")
app.add_typer(gateway_cmd.app, name="gateway", help="Run the gateway server.")
app.add_typer(message_cmd.app, name="message", help="Send a message via the gateway.")
app.add_typer(skills_cmd.app, name="skills", help="Browse/install skills from ClawHub.")
app.add_typer(memory_cmd.app, name="memory", help="Manage long-term memory.")
app.add_typer(sessions_cmd.app, name="session", help="Inspect and manage agent sessions.")
app.add_typer(wiki_cmd.app, name="wiki", help="Browse and curate the durable knowledge wiki.")
app.add_typer(backup_cmd.app, name="backup", help="Backup and restore the home directory.")


@app.command()
def version() -> None:
    """Print sampyclaw version."""
    from sampyclaw import __version__

    typer.echo(__version__)


@app.command()
def paths() -> None:
    """Print resolved filesystem paths (config, credentials, agents)."""
    from sampyclaw.config import default_paths

    p = default_paths()
    typer.echo(f"home:         {p.home}")
    typer.echo(f"config_file:  {p.config_file}")
    typer.echo(f"credentials:  {p.credentials_dir}")
    typer.echo(f"agents:       {p.agents_dir}")
    typer.echo(f"plugins:      {p.plugins_dir}")


if __name__ == "__main__":
    app()
