"""Phase 14: sessions.* RPC + CLI."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from oxenclaw.cli.sessions_cmd import app as sessions_app
from oxenclaw.gateway.router import Router
from oxenclaw.gateway.sessions_methods import register_sessions_methods
from oxenclaw.pi import (
    AssistantMessage,
    CreateAgentSessionOptions,
    SystemMessage,
    TextContent,
    UserMessage,
)
from oxenclaw.pi.lifecycle import LifecycleBus
from oxenclaw.pi.persistence import SQLiteSessionManager
from oxenclaw.pi.policy import (
    SessionChatType,
    SessionPolicy,
    get_policy,
    serialize_policy,
)


async def _seed(sm: SQLiteSessionManager, *, n: int = 3) -> list[str]:
    ids = []
    for i in range(n):
        s = await sm.create(CreateAgentSessionOptions(agent_id="x", title=f"s{i}"))
        s.messages = [
            SystemMessage(content="be brief"),
            UserMessage(content=f"u{i} question"),
            AssistantMessage(content=[TextContent(text=f"a{i} answer")], stop_reason="end_turn"),
        ]
        await sm.save(s)
        ids.append(s.id)
    return ids


# ─── RPC: list / get / preview ───────────────────────────────────────


async def test_sessions_list_filters_by_agent(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "s.db")
    await _seed(sm, n=2)
    other = await sm.create(CreateAgentSessionOptions(agent_id="other"))
    await sm.save(other)
    router = Router()
    register_sessions_methods(router, sm, archive_dir=tmp_path / "ar")

    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "sessions.list", "params": {"agent_id": "x"}}
    )
    assert resp.error is None
    assert all(r["agent_id"] == "x" for r in resp.result)
    assert len(resp.result) == 2
    sm.close()


async def test_sessions_list_includes_dashboard_compat_fields(tmp_path: Path) -> None:
    """Dashboard reads `session_key`, `archived`, and previews — list must populate them."""
    sm = SQLiteSessionManager(tmp_path / "s.db")
    ids = await _seed(sm, n=1)
    router = Router()
    register_sessions_methods(router, sm)

    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "sessions.list", "params": {}}
    )
    assert resp.error is None
    assert len(resp.result) == 1
    row = resp.result[0]
    assert row["id"] == ids[0]
    assert row["session_key"] == ids[0]
    assert row["archived"] is False
    assert row["first_preview"].startswith("u0 ")
    assert row["last_preview"].endswith("answer")
    sm.close()


async def test_sessions_get_returns_full_payload(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "s.db")
    ids = await _seed(sm, n=1)
    router = Router()
    register_sessions_methods(router, sm)
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "sessions.get", "params": {"id": ids[0]}}
    )
    assert resp.error is None
    payload = resp.result
    assert payload["id"] == ids[0]
    assert len(payload["messages"]) == 3
    sm.close()


async def test_sessions_preview_summary(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "s.db")
    ids = await _seed(sm, n=1)
    router = Router()
    register_sessions_methods(router, sm)
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "sessions.preview", "params": {"id": ids[0]}}
    )
    p = resp.result
    assert p["first_user"].startswith("u0 ")
    assert p["last_assistant"].endswith("answer")
    assert p["message_count"] == 3
    sm.close()


async def test_sessions_get_missing_returns_null(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "s.db")
    router = Router()
    register_sessions_methods(router, sm)
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "sessions.get", "params": {"id": "missing"}}
    )
    assert resp.error is None
    assert resp.result is None
    sm.close()


# ─── RPC: patch / reset / fork / archive / delete ───────────────────


async def test_sessions_patch_updates_title_and_policy(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "s.db")
    ids = await _seed(sm, n=1)
    router = Router()
    register_sessions_methods(router, sm)

    new_policy = SessionPolicy(chat_type=SessionChatType.GROUP)
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sessions.patch",
            "params": {
                "id": ids[0],
                "title": "renamed",
                "policy": serialize_policy(new_policy),
            },
        }
    )
    assert resp.error is None
    fetched = await sm.get(ids[0])
    assert fetched is not None
    assert fetched.title == "renamed"
    assert get_policy(fetched).chat_type is SessionChatType.GROUP
    sm.close()


async def test_sessions_reset_drops_messages(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "s.db")
    ids = await _seed(sm, n=1)
    router = Router()
    register_sessions_methods(router, sm)
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sessions.reset",
            "params": {"id": ids[0], "full": True, "keep_system": True},
        }
    )
    assert resp.result["messages_remaining"] == 1
    sm.close()


async def test_sessions_fork_creates_branch(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "s.db")
    ids = await _seed(sm, n=1)
    router = Router()
    register_sessions_methods(router, sm)
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sessions.fork",
            "params": {"id": ids[0]},
        }
    )
    new_id = resp.result["id"]
    assert new_id != ids[0]
    fetched = await sm.get(new_id)
    assert fetched is not None
    assert fetched.metadata.get("forked_from") == ids[0]
    sm.close()


async def test_sessions_archive_writes_and_deletes(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "s.db")
    ids = await _seed(sm, n=1)
    router = Router()
    register_sessions_methods(router, sm, archive_dir=tmp_path / "arch")
    resp = await router.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sessions.archive",
            "params": {"id": ids[0]},
        }
    )
    assert "archive_path" in resp.result
    assert await sm.get(ids[0]) is None
    sm.close()


async def test_sessions_delete(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "s.db")
    ids = await _seed(sm, n=1)
    router = Router()
    register_sessions_methods(router, sm)
    resp = await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "sessions.delete", "params": {"id": ids[0]}}
    )
    assert resp.result == {"deleted": True}
    sm.close()


# ─── lifecycle bus integration ──────────────────────────────────────


async def test_reset_emits_lifecycle_event_via_bus(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "s.db")
    ids = await _seed(sm, n=1)
    bus = LifecycleBus()
    seen: list = []

    async def _h(kind, payload):  # type: ignore[no-untyped-def]
        seen.append((kind, payload))

    bus.subscribe(_h)
    router = Router()
    register_sessions_methods(router, sm, bus=bus)
    await router.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "sessions.reset", "params": {"id": ids[0]}}
    )
    assert seen and seen[0][0].value == "session.reset"
    sm.close()


# ─── CLI ─────────────────────────────────────────────────────────────


def test_cli_list_then_show(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))

    # Pre-seed via direct store under the same path the CLI uses.
    import asyncio

    async def _setup() -> str:
        sm = SQLiteSessionManager(tmp_path / "sessions.db")
        s = await sm.create(CreateAgentSessionOptions(agent_id="x", title="cli-t"))
        s.messages = [UserMessage(content="hello cli")]
        await sm.save(s)
        sm.close()
        return s.id

    sid = asyncio.run(_setup())

    runner = CliRunner()
    result = runner.invoke(sessions_app, ["list"])
    assert result.exit_code == 0
    assert "cli-t" in result.stdout

    result = runner.invoke(sessions_app, ["show", sid, "--no-messages"])
    assert result.exit_code == 0
    assert sid in result.stdout
    assert "messages:  1" in result.stdout
