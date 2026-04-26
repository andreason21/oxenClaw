"""Tests for chat.history + chat.clear RPCs."""

from __future__ import annotations

from oxenclaw.agents.history import ConversationHistory
from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.gateway.chat_methods import register_chat_methods
from oxenclaw.gateway.router import Router


def _setup(tmp_path):  # type: ignore[no-untyped-def]
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    router = Router()
    register_chat_methods(router, paths=paths)
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
