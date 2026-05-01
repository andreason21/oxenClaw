"""Top-level `oxenclaw` CLI.

Port of openclaw `src/cli/*`. Uses typer for type-driven argument parsing.
Subcommands live in sibling modules.
"""

from __future__ import annotations

import typer

from oxenclaw.cli import (
    acp_cmd,
    backup_cmd,
    config_cmd,
    flows_cmd,
    gateway_cmd,
    memory_cmd,
    message_cmd,
    sessions_cmd,
    skills_cmd,
    wiki_cmd,
)

app = typer.Typer(
    help="oxenclaw — Python port of openclaw.",
    no_args_is_help=True,
    rich_markup_mode="markdown",
)


@app.callback()
def _root_callback() -> None:
    """Runs before any subcommand. Autoloads `~/.oxenclaw/env` so the
    `--provider auto` resolver (and any other env-driven path) sees
    values persisted by `oxenclaw setup llamacpp` even when the user
    didn't `source` the file in their shell. Shell-set vars still win.
    """
    from oxenclaw.config.env_loader import load_oxenclaw_env_file

    load_oxenclaw_env_file()


app.add_typer(config_cmd.app, name="config", help="Inspect and edit config.yaml.")
app.add_typer(
    gateway_cmd.app,
    name="gateway",
    help=(
        "Run the gateway server. Most behaviour is config-driven via "
        "$OXENCLAW_HOME/config.yaml — see `oxenclaw gateway start --help` "
        "for the full list of CLI overrides and env vars."
    ),
)
app.add_typer(message_cmd.app, name="message", help="Send a message via the gateway.")
app.add_typer(skills_cmd.app, name="skills", help="Browse/install skills from ClawHub.")
app.add_typer(memory_cmd.app, name="memory", help="Manage long-term memory.")
app.add_typer(sessions_cmd.app, name="session", help="Inspect and manage agent sessions.")
app.add_typer(wiki_cmd.app, name="wiki", help="Browse and curate the durable knowledge wiki.")
app.add_typer(backup_cmd.app, name="backup", help="Backup and restore the home directory.")
app.add_typer(
    acp_cmd.app,
    name="acp",
    help="Run oxenclaw as an ACP agent over stdio (for IDEs like Zed).",
)
app.add_typer(
    flows_cmd.doctor_app, name="doctor", help="Aggregated health check across subsystems."
)
app.add_typer(
    flows_cmd.setup_app,
    name="setup",
    help="Interactive setup wizards (model / provider / channel).",
)


@app.command()
def version() -> None:
    """Print oxenclaw version."""
    from oxenclaw import __version__

    typer.echo(__version__)


@app.command()
def paths() -> None:
    """Print resolved filesystem paths (config, credentials, agents)."""
    from oxenclaw.config import default_paths

    p = default_paths()
    typer.echo(f"home:         {p.home}")
    typer.echo(f"config_file:  {p.config_file}")
    typer.echo(f"credentials:  {p.credentials_dir}")
    typer.echo(f"agents:       {p.agents_dir}")
    typer.echo(f"plugins:      {p.plugins_dir}")


if __name__ == "__main__":
    app()
