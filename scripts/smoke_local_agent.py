"""Manual smoke test against a real local Ollama running Gemma.

This is a thin wrapper around the canonical integration test suite at
`tests/integration/test_local_agent_ollama.py`. Prefer the pytest path:

    OLLAMA_INTEGRATION=1 pytest tests/integration/ -v

The pytest version reports per-scenario pass/fail, runs in CI when opted in,
and is the source of truth. This script remains for one-shot ad-hoc runs
where verbose human-readable timing output is convenient.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from oxenclaw.agents import (
    AgentContext,
    LocalAgent,
    ToolRegistry,
    default_tools,
)
from oxenclaw.agents.history import ConversationHistory
from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.plugin_sdk.channel_contract import ChannelTarget, InboundEnvelope


def _check_ollama_reachable(base_url: str) -> None:
    health = base_url.replace("/v1", "") + "/api/tags"
    try:
        with urllib.request.urlopen(health, timeout=5) as resp:
            resp.read(1)
    except (urllib.error.URLError, OSError) as exc:
        sys.exit(f"FAIL: cannot reach Ollama at {health}: {exc}")


def _envelope(text: str) -> InboundEnvelope:
    return InboundEnvelope(
        channel="dashboard",
        account_id="main",
        target=ChannelTarget(channel="dashboard", account_id="main", chat_id="42"),
        sender_id="smoke",
        text=text,
        received_at=time.time(),
    )


async def _collect(agent: LocalAgent, env: InboundEnvelope, ctx: AgentContext) -> str:
    chunks: list[str] = []
    async for sp in agent.handle(env, ctx):
        chunks.append(sp.text or "")
    return "".join(chunks)


async def main(model: str, base_url: str) -> int:
    print(f"== smoke test :: {model} @ {base_url} ==", flush=True)
    _check_ollama_reachable(base_url)

    home = Path("/tmp/oxenclaw-smoke")
    home.mkdir(exist_ok=True)
    paths = OxenclawPaths(home=home)
    paths.ensure_home()
    sessions_dir = paths.agent_dir("local-smoke") / "sessions"
    if sessions_dir.exists():
        for p in sessions_dir.glob("*.json"):
            p.unlink()

    tools = ToolRegistry()
    tools.register_all(default_tools())

    agent = LocalAgent(
        agent_id="local-smoke",
        model=model,
        base_url=base_url,
        tools=tools,
        paths=paths,
        timeout=120.0,
    )

    failures: list[str] = []
    try:
        ctx = AgentContext(agent_id=agent.id, session_key="plain")
        t0 = time.time()
        out = await _collect(agent, _envelope("Reply with exactly the word OK."), ctx)
        print(f"[1] plain text reply ({time.time() - t0:.1f}s):\n    {out!r}", flush=True)
        if not out.strip():
            failures.append("empty plain-text reply")

        ctx2 = AgentContext(agent_id=agent.id, session_key="tooluse")
        t0 = time.time()
        out2 = await _collect(
            agent,
            _envelope(
                "What is the current UTC time? Use the get_time tool and report the result."
            ),
            ctx2,
        )
        print(f"[2] tool-use reply ({time.time() - t0:.1f}s):\n    {out2!r}", flush=True)
        if not out2.strip():
            failures.append("empty tool-use reply")

        ctx3 = AgentContext(agent_id=agent.id, session_key="multi")
        await _collect(
            agent, _envelope("My favourite colour is teal. Remember that."), ctx3
        )
        out3 = await _collect(
            agent, _envelope("What did I say my favourite colour was?"), ctx3
        )
        print(f"[3] multi-turn recall:\n    {out3!r}", flush=True)
        if "teal" not in out3.lower():
            failures.append(f"multi-turn forgot 'teal': {out3!r}")

        hist = ConversationHistory(paths.session_file(agent.id, "multi"))
        print(f"[3] persisted multi history has {len(hist)} messages", flush=True)
        if len(hist) < 5:
            failures.append(f"history short: {len(hist)}")
    finally:
        await agent.aclose()

    if failures:
        print("\nFAILURES:", flush=True)
        for f in failures:
            print(f"  - {f}", flush=True)
        return 1
    print("\nALL OK", flush=True)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="gemma4:latest")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.model, args.base_url)))
