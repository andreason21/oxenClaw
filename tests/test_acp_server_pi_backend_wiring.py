"""Production-path wiring test for `oxenclaw acp --backend pi`.

The server's `_build_runtime("pi")` factory was originally a memoryless
PiAgent. That meant an ACP client could say "remember X" or "what is
X" and the agent literally had no `memory_save` / `memory_search`
tool to call — and no MemoryRetriever to write to even if it had one.

This test pins the fix: the pi backend must register the three
memory tools on the agent's ToolRegistry whenever a retriever can be
built. The retriever construction itself is best-effort (graceful
degrade if the operator's env can't reach an embedder) but when it
succeeds, the tools must be wired.

Why this test matters: when an operator types `oxenclaw acp --backend
pi` and a downstream client (Zed, another oxenclaw) connects, the
agent should behave like the gateway-hosted variant. Skipping the
memory wiring here was the silent gap that broke the
"나는 수원 살아 → 내가 사는 곳 날씨 알려줘" flow at the production
layer.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_pi_backend_factory_registers_memory_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boot the pi backend factory in isolation and assert the agent
    holds the three memory tools."""
    # Redirect default_paths to tmp_path so the test doesn't write
    # into the real ~/.oxenclaw home.
    from oxenclaw.config import paths as paths_mod

    monkeypatch.setattr(
        paths_mod,
        "default_paths",
        lambda: paths_mod.OxenclawPaths(home=tmp_path),
    )
    from oxenclaw.config import default_paths as cfg_default_paths

    monkeypatch.setattr(
        "oxenclaw.config.default_paths",
        lambda: paths_mod.OxenclawPaths(home=tmp_path),
    )

    # Stub build_embedder so we don't need a real embedder env.
    from oxenclaw.memory import retriever as retriever_mod
    from tests._memory_stubs import StubEmbeddings

    monkeypatch.setattr(
        "oxenclaw.memory.build_embedder",
        lambda *a, **k: StubEmbeddings(),
    )

    # Now boot the factory.
    from oxenclaw.acp.server import _build_runtime
    from oxenclaw.acp.pi_agent_runtime import PiAgentAcpRuntime

    runtime = _build_runtime("pi")
    assert isinstance(runtime, PiAgentAcpRuntime)
    agent = runtime._agent
    tool_names = set(agent._tools._tools.keys())
    assert "memory_save" in tool_names, (
        "pi backend must register memory_save on the agent's tool "
        f"registry. Got: {sorted(tool_names)}"
    )
    assert "memory_search" in tool_names
    assert "memory_get" in tool_names

    # MemoryRetriever must be wired into PiAgent itself so the
    # user-side recall prelude path can fire on subsequent turns.
    assert agent._memory is not None, (
        "pi backend must attach a MemoryRetriever to the PiAgent so "
        "_build_user_recall_prelude can inject prior facts."
    )
