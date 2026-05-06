"""`oxenclaw message` subcommands — talk to a running gateway as a JSON-RPC client."""

from __future__ import annotations

import asyncio
import json
import os

import typer
from websockets.asyncio.client import connect

app = typer.Typer(help="Send messages via a running gateway.", no_args_is_help=True)


def _resolve_token(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    env = os.environ.get("OXENCLAW_GATEWAY_TOKEN")
    return env.strip() if env and env.strip() else None


def _connect_kwargs(token: str | None) -> dict[str, object]:
    if not token:
        return {}
    return {"additional_headers": {"Authorization": f"Bearer {token}"}}


@app.command("send")
def send(
    text: str = typer.Argument(..., help="Message text."),
    channel: str = typer.Option("dashboard", help="Channel id."),
    account_id: str = typer.Option("main", help="Account id."),
    chat_id: str = typer.Option(..., help="Destination chat id."),
    thread_id: str | None = typer.Option(None, help="Thread/topic id (optional)."),
    agent_id: str | None = typer.Option(
        None, help="Pin the dispatch to a specific agent id (optional)."
    ),
    gateway: str = typer.Option("ws://127.0.0.1:7331", help="Gateway WebSocket URL."),
    auth_token: str | None = typer.Option(
        None,
        "--auth-token",
        help=(
            "Bearer token for the gateway. Falls back to "
            "$OXENCLAW_GATEWAY_TOKEN. Sent as `Authorization: Bearer …` "
            "on the WS upgrade."
        ),
    ),
) -> None:
    """Send a `chat.send` JSON-RPC call to a running gateway."""
    token = _resolve_token(auth_token)

    async def _run() -> None:
        async with connect(gateway, **_connect_kwargs(token)) as ws:
            await ws.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "chat.send",
                        "params": {
                            "channel": channel,
                            "account_id": account_id,
                            "chat_id": chat_id,
                            "thread_id": thread_id,
                            "agent_id": agent_id,
                            "text": text,
                        },
                    }
                )
            )
            response = await ws.recv()
            typer.echo(response)

    asyncio.run(_run())


@app.command("agents")
def agents(
    gateway: str = typer.Option("ws://127.0.0.1:7331", help="Gateway WebSocket URL."),
    auth_token: str | None = typer.Option(
        None,
        "--auth-token",
        help=(
            "Bearer token for the gateway. Falls back to "
            "$OXENCLAW_GATEWAY_TOKEN."
        ),
    ),
) -> None:
    """List agents registered on a running gateway."""
    token = _resolve_token(auth_token)

    async def _run() -> None:
        async with connect(gateway, **_connect_kwargs(token)) as ws:
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "agents.list"}))
            typer.echo(await ws.recv())

    asyncio.run(_run())
