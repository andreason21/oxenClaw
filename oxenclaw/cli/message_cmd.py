"""`oxenclaw message` subcommands — talk to a running gateway as a JSON-RPC client."""

from __future__ import annotations

import asyncio
import json

import typer
from websockets.asyncio.client import connect

app = typer.Typer(help="Send messages via a running gateway.", no_args_is_help=True)


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
) -> None:
    """Send a `chat.send` JSON-RPC call to a running gateway."""

    async def _run() -> None:
        async with connect(gateway) as ws:
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
) -> None:
    """List agents registered on a running gateway."""

    async def _run() -> None:
        async with connect(gateway) as ws:
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "agents.list"}))
            typer.echo(await ws.recv())

    asyncio.run(_run())
