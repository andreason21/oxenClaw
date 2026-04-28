"""Tests for chat.history + chat.clear + chat.debug_prompt RPCs."""

from __future__ import annotations

from oxenclaw.agents import AgentRegistry
from oxenclaw.agents.history import ConversationHistory
from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.gateway.chat_methods import register_chat_methods
from oxenclaw.gateway.router import Router


def _setup(tmp_path, *, agents: AgentRegistry | None = None):  # type: ignore[no-untyped-def]
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    router = Router()
    register_chat_methods(router, paths=paths, agents=agents)
    return router, paths


def _populate(paths, agent_id="a", session_key="s", count=3) -> None:  # type: ignore[no-untyped-def]
    hist = ConversationHistory(paths.session_file(agent_id, session_key))
    for i in range(count):
        hist.append({"role": "user", "content": f"msg-{i}"})
    hist.save()


async def test_history_returns_all_messages(tmp_path) -> None:  # type: ignore[no-untyped-def]
    router, paths = _setup(tmp_path)
    _populate(paths, count=3)
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "chat.history",
            "params": {"agent_id": "a", "session_key": "s"},
        }
    )
    assert resp.error is None
    assert resp.result["total"] == 3
    assert len(resp.result["messages"]) == 3


async def test_history_limit_returns_tail(tmp_path) -> None:  # type: ignore[no-untyped-def]
    router, paths = _setup(tmp_path)
    _populate(paths, count=5)
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "chat.history",
            "params": {"agent_id": "a", "session_key": "s", "limit": 2},
        }
    )
    texts = [m["content"] for m in resp.result["messages"]]
    assert texts == ["msg-3", "msg-4"]
    assert resp.result["total"] == 5


async def test_history_missing_file_is_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    router, _ = _setup(tmp_path)
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "chat.history",
            "params": {"agent_id": "none", "session_key": "none"},
        }
    )
    assert resp.result == {"messages": [], "total": 0}


async def test_clear_removes_session_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    router, paths = _setup(tmp_path)
    _populate(paths)
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "chat.clear",
            "params": {"agent_id": "a", "session_key": "s"},
        }
    )
    assert resp.result == {"cleared": True}
    # second clear is a no-op
    resp2 = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "chat.clear",
            "params": {"agent_id": "a", "session_key": "s"},
        }
    )
    assert resp2.result == {"cleared": False}


async def test_history_rejects_bad_params(tmp_path) -> None:  # type: ignore[no-untyped-def]
    router, _ = _setup(tmp_path)
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "chat.history", "params": {"agent_id": "a"}}
    )
    assert resp.error is not None


async def test_list_sessions_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    router, _ = _setup(tmp_path)
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "chat.list_sessions",
            "params": {"agent_id": "ghost"},
        }
    )
    assert resp.result == {"sessions": []}


async def test_list_sessions_returns_metadata_sorted_recent_first(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import os
    import time

    router, paths = _setup(tmp_path)
    sessions_dir = paths.agent_dir("a") / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "old.json").write_text('{"messages": []}')
    (sessions_dir / "new.json").write_text('{"messages": [{"role":"user","content":"x"}]}')
    os.utime(sessions_dir / "old.json", (time.time() - 1000, time.time() - 1000))

    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "chat.list_sessions",
            "params": {"agent_id": "a"},
        }
    )
    keys = [s["session_key"] for s in resp.result["sessions"]]
    assert keys == ["new", "old"]
    assert all("size" in s and "modified_at" in s for s in resp.result["sessions"])


# ─── chat.debug_prompt ───────────────────────────────────────────────


class _FakePiAgent:
    id = "pi"

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    async def debug_assemble(self, query: str) -> dict:
        if self._fail:
            raise RuntimeError("boom")
        return {
            "model_id": "fake-model",
            "agent_id": "pi",
            "system_prompt": f"BASE\n\n<recalled>{query}</recalled>",
            "system_prompt_chars": 32,
            "base_prompt_chars": 4,
            "memory_hits": [{"chunk_id": "c1", "score": 0.42}],
            "memory_block": "<recalled>...</recalled>",
            "memory_block_chars": 24,
            "memory_weak_threshold": 0.30,
            "skills_block": "",
            "skills_block_chars": 0,
        }


async def test_debug_prompt_returns_assembled_payload(tmp_path) -> None:  # type: ignore[no-untyped-def]
    agents = AgentRegistry()
    agents.register(_FakePiAgent())
    router, _ = _setup(tmp_path, agents=agents)
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "chat.debug_prompt",
            "params": {"agent_id": "pi", "query": "내가 어디 살지?"},
        }
    )
    assert resp.result["ok"] is True
    assert "BASE" in resp.result["system_prompt"]
    assert resp.result["model_id"] == "fake-model"
    assert resp.result["memory_hits"][0]["chunk_id"] == "c1"


async def test_debug_prompt_unknown_agent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    agents = AgentRegistry()
    router, _ = _setup(tmp_path, agents=agents)
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "chat.debug_prompt",
            "params": {"agent_id": "ghost", "query": "x"},
        }
    )
    assert resp.result["ok"] is False
    assert "not registered" in resp.result["error"]


async def test_debug_prompt_agent_without_debug_method(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Echo / non-Pi agents lack debug_assemble — RPC returns structured
    error so the dashboard renders 'unsupported' rather than crashing."""
    agents = AgentRegistry()
    from oxenclaw.agents import build_agent

    agents.register(build_agent(agent_id="echoer", provider="echo"))
    router, _ = _setup(tmp_path, agents=agents)
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "chat.debug_prompt",
            "params": {"agent_id": "echoer", "query": "x"},
        }
    )
    assert resp.result["ok"] is False
    assert "debug_assemble" in resp.result["error"]


async def test_debug_prompt_handles_internal_failure(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A debug_assemble that raises must be surfaced as a structured
    error, never as a 500."""
    agents = AgentRegistry()
    agents.register(_FakePiAgent(fail=True))
    router, _ = _setup(tmp_path, agents=agents)
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "chat.debug_prompt",
            "params": {"agent_id": "pi", "query": "x"},
        }
    )
    assert resp.result["ok"] is False
    assert "boom" in resp.result["error"]


async def test_debug_prompt_unregistered_when_agents_omitted(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """If the gateway didn't pass agents= to register_chat_methods, the
    method should not be registered at all (not silently no-op)."""
    router, _ = _setup(tmp_path)  # no agents arg
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "chat.debug_prompt",
            "params": {"agent_id": "pi", "query": "x"},
        }
    )
    assert resp.error is not None  # method-not-found
