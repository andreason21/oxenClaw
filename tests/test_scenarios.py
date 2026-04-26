"""End-to-end integration scenarios across pi + session phases.

Each scenario exercises multiple subsystems together rather than testing
modules in isolation. They are the "does this actually work as a system?"
suite — failures here often indicate wiring/contract gaps even when every
unit test passes.

Scenarios:
1. `test_scenario_conversation_to_archive` — user → tools → compaction →
   archive → restore.
2. `test_scenario_cache_observability_warm_then_drop` — cache markers
   warm on early turns, get dropped once hit_rate stays low.
3. `test_scenario_group_chat_send_policy_gates_inbound` — SendPolicy
   filters group messages while DMs flow through.
4. `test_scenario_fork_diverges_independently` — fork at turn N, both
   sides evolve without affecting each other.
5. `test_scenario_memory_recall_across_sessions` — index session A via
   SessionMemoryHook, retrieve from MemoryRetriever in a fresh session B.
6. `test_scenario_failover_after_persistent_provider_error` — primary
   model fails repeatedly → classifier picks failover → runner uses it.
7. `test_scenario_disk_budget_prunes_oldest_sessions` — maintenance task
   keeps the store under budget while preserving keep_min recent rows.
8. `test_scenario_cli_full_session_lifecycle` — list → show → fork →
   archive → restore via the CLI.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

import oxenclaw.pi.providers  # noqa: F401  registers stream wrappers
from oxenclaw.agents.base import AgentContext
from oxenclaw.agents.pi_agent import PiAgent
from oxenclaw.agents.tools import FunctionTool, ToolRegistry
from oxenclaw.cli.sessions_cmd import app as sessions_cli_app
from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.memory.embedding_cache import EmbeddingCache
from oxenclaw.memory.retriever import MemoryRetriever
from oxenclaw.memory.store import MemoryStore
from oxenclaw.pi import (
    AssistantMessage,
    CreateAgentSessionOptions,
    InMemoryAuthStorage,
    Model,
    SystemMessage,
    TextContent,
    UserMessage,
    register_provider_stream,
)
from oxenclaw.pi.cache_observability import (
    CacheObserver,
    should_apply_cache_markers,
)
from oxenclaw.pi.extras import classify_failure, select_failover_model
from oxenclaw.pi.lifecycle import (
    ForkOptions,
    LifecycleBus,
    LifecycleEvent,
    archive_session,
    fork_session,
    restore_archive,
)
from oxenclaw.pi.persistence import SQLiteSessionManager
from oxenclaw.pi.policy import (
    SendMode,
    SendPolicy,
    SessionChatType,
    SessionPolicy,
)
from oxenclaw.pi.registry import InMemoryModelRegistry
from oxenclaw.pi.run import RuntimeConfig, run_agent_turn
from oxenclaw.pi.session_memory_hook import SessionMemoryHook
from oxenclaw.pi.store_ops import (
    MaintenanceConfig,
    StoreMaintenance,
    db_size_bytes,
)
from oxenclaw.pi.streaming import (
    ErrorEvent,
    StopEvent,
    TextDeltaEvent,
    ToolUseEndEvent,
    ToolUseInputDeltaEvent,
    ToolUseStartEvent,
    UsageEvent,
)
from oxenclaw.plugin_sdk.channel_contract import ChannelTarget, InboundEnvelope
from tests._memory_stubs import StubEmbeddings

# ─── helpers ─────────────────────────────────────────────────────────


def _paths(tmp_path: Path) -> OxenclawPaths:
    p = OxenclawPaths(home=tmp_path)
    p.ensure_home()
    return p


def _model(provider: str, *, ctx: int = 100_000, max_out: int = 256) -> Model:
    return Model(
        id=f"m-{provider}",
        provider=provider,
        context_window=ctx,
        max_output_tokens=max_out,
        supports_prompt_cache=True,
        extra={"base_url": "http://test-fake"},
    )


def _registry(*models: Model) -> InMemoryModelRegistry:
    return InMemoryModelRegistry(models=list(models))


def _auth(provider: str) -> InMemoryAuthStorage:
    return InMemoryAuthStorage({provider: "sk-test"})  # type: ignore[dict-item]


def _inbound(text: str, *, chat_id: str = "42") -> InboundEnvelope:
    return InboundEnvelope(
        channel="telegram",
        account_id="main",
        target=ChannelTarget(channel="telegram", account_id="main", chat_id=chat_id),
        sender_id="user-1",
        text=text,
        received_at=0.0,
    )


# ═══════════════════════════════════════════════════════════════════════
# Scenario 1: full conversation → tools → compaction → archive → restore
# ═══════════════════════════════════════════════════════════════════════


async def test_scenario_conversation_to_archive(tmp_path: Path) -> None:
    """A multi-turn dialog with a tool call, then archived to disk and
    restored — every byte of transcript survives the round-trip."""
    state = {"turn": 0}

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        state["turn"] += 1
        if state["turn"] == 1:
            # First turn: assistant answers directly.
            yield TextDeltaEvent(delta="hello there")
            yield UsageEvent(usage={"input_tokens": 50, "output_tokens": 5})
            yield StopEvent(reason="end_turn")
        elif state["turn"] == 2:
            # Second turn: assistant calls a tool.
            yield ToolUseStartEvent(id="t1", name="echo")
            yield ToolUseInputDeltaEvent(id="t1", input_delta='{"text":"world"}')
            yield ToolUseEndEvent(id="t1")
            yield UsageEvent(usage={"input_tokens": 80, "output_tokens": 10})
            yield StopEvent(reason="tool_use")
        else:
            # Third + final.
            yield TextDeltaEvent(delta="tool said: world")
            yield UsageEvent(usage={"input_tokens": 90, "output_tokens": 6})
            yield StopEvent(reason="end_turn")

    register_provider_stream("scn1_provider", fake_stream)

    class _Args(BaseModel):
        text: str

    async def echo_handler(args: _Args) -> str:
        return f"echoed: {args.text}"

    tools = ToolRegistry()
    tools.register(
        FunctionTool(name="echo", description="echo", input_model=_Args, handler=echo_handler)
    )

    sm = SQLiteSessionManager(tmp_path / "sess.db")
    bus = LifecycleBus()
    seen_events: list[tuple] = []

    async def _on_event(kind, payload):  # type: ignore[no-untyped-def]
        seen_events.append((kind, payload))

    bus.subscribe(_on_event)

    agent = PiAgent(
        agent_id="t",
        model_id="m-scn1_provider",
        registry=_registry(_model("scn1_provider")),
        auth=_auth("scn1_provider"),
        sessions=sm,
        tools=tools,
        paths=_paths(tmp_path),
    )
    ctx = AgentContext(agent_id="t", session_key="conv1")

    # Turn 1: greeting.
    out1 = [sp async for sp in agent.handle(_inbound("hi"), ctx)]
    assert out1 and "hello there" in out1[0].text

    # Turn 2 → 3: tool round-trip.
    out2 = [sp async for sp in agent.handle(_inbound("compute"), ctx)]
    assert out2 and "tool said: world" in out2[0].text

    # Validate persistence.
    [entry] = await sm.list(agent_id="t")
    fetched = await sm.get(entry.id)
    assert fetched is not None
    # Expect: 1 user, 1 asst, 1 user, 1 asst(tool_use), 1 tool_result, 1 asst.
    assert len(fetched.messages) == 6

    # Archive (which deletes live row) and restore.
    res = await archive_session(sm, fetched.id, archive_dir=tmp_path / "arch", bus=bus)
    assert res is not None and res.archive_path.exists()
    assert await sm.get(fetched.id) is None
    restored = await restore_archive(sm, res.archive_path)
    assert restored is not None
    again = await sm.get(restored.id)
    assert again is not None and len(again.messages) == 6

    archived_kinds = [k for (k, _) in seen_events]
    assert LifecycleEvent.ARCHIVED in archived_kinds
    sm.close()


# ═══════════════════════════════════════════════════════════════════════
# Scenario 2: cache observability — warm-up, then drop markers
# ═══════════════════════════════════════════════════════════════════════


async def test_scenario_cache_observability_warm_then_drop(
    tmp_path: Path,
) -> None:
    """Simulate 5 turns. First 3 burn input tokens; later turns get no
    cache hits → policy decides to stop emitting cache markers."""
    obs = CacheObserver()
    # Three early turns: pure misses.
    for _ in range(3):
        obs.record({"input_tokens": 5_000, "cache_read_input_tokens": 0})
        # During warmup window the policy still says "yes apply markers".
    # After warmup, with persistent miss + no recent hit, drop markers.
    obs.record({"input_tokens": 5_000, "cache_read_input_tokens": 0})
    assert should_apply_cache_markers(obs) is False

    # Now imagine a turn finally hits cache → decision flips back to True.
    obs.record({"input_tokens": 100, "cache_read_input_tokens": 50_000})
    assert should_apply_cache_markers(obs) is True
    assert obs.hit_rate() > 0.5
    assert obs.cache_alive() is True


# ═══════════════════════════════════════════════════════════════════════
# Scenario 3: SendPolicy gates group messages while DMs flow through
# ═══════════════════════════════════════════════════════════════════════


async def test_scenario_group_chat_send_policy_gates_inbound() -> None:
    """ALLOWLIST policy: only sender 'alice' is allowed to address the bot
    in a group; everyone is fine in DM."""
    pol = SessionPolicy(
        chat_type=SessionChatType.GROUP,
        send=SendPolicy(mode=SendMode.ALLOWLIST, allow=("alice",), deny=("bob",)),
    )
    p = pol.send

    # Group: alice ok, bob denied (also in deny), carol blocked by mode.
    assert (
        p.should_reply(chat_type=SessionChatType.GROUP, sender_id="alice", text="@bot hi") is True
    )
    assert p.should_reply(chat_type=SessionChatType.GROUP, sender_id="bob", text="hey") is False
    assert p.should_reply(chat_type=SessionChatType.GROUP, sender_id="carol", text="hey") is False

    # Switch to DM_ONLY: only DM is allowed regardless of allow/deny.
    p2 = SendPolicy(mode=SendMode.DM_ONLY)
    assert p2.should_reply(chat_type=SessionChatType.DM, sender_id="alice", text="x") is True
    assert p2.should_reply(chat_type=SessionChatType.GROUP, sender_id="alice", text="x") is False

    # ADDRESSED_ONLY: needs mention or reply-to-bot.
    p3 = SendPolicy(mode=SendMode.ADDRESSED_ONLY, addressed_handles=("@oxenclaw",))
    assert (
        p3.should_reply(chat_type=SessionChatType.GROUP, sender_id="anyone", text="random chat")
        is False
    )
    assert (
        p3.should_reply(chat_type=SessionChatType.GROUP, sender_id="anyone", text="@oxenclaw help")
        is True
    )
    assert (
        p3.should_reply(
            chat_type=SessionChatType.GROUP, sender_id="anyone", text="ok", is_reply_to_bot=True
        )
        is True
    )


# ═══════════════════════════════════════════════════════════════════════
# Scenario 4: fork at turn N — both sides evolve independently
# ═══════════════════════════════════════════════════════════════════════


async def test_scenario_fork_diverges_independently(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "fork.db")
    src = await sm.create(CreateAgentSessionOptions(agent_id="x", title="src"))
    src.messages = [
        SystemMessage(content="be helpful"),
        UserMessage(content="design a database"),
        AssistantMessage(content=[TextContent(text="use sqlite")], stop_reason="end_turn"),
        UserMessage(content="follow up question"),
        AssistantMessage(content=[TextContent(text="here's more detail")], stop_reason="end_turn"),
    ]
    await sm.save(src)

    # Fork after turn 1 (index 2 = first assistant). The fork keeps system
    # + first user + first assistant.
    fork = await fork_session(sm, src.id, options=ForkOptions(until_index=2))
    assert fork is not None
    assert len(fork.messages) == 3

    # Mutate the fork: append a different turn.
    fork.messages.append(UserMessage(content="alternate path"))
    fork.messages.append(
        AssistantMessage(content=[TextContent(text="postgres instead")], stop_reason="end_turn")
    )
    await sm.save(fork)

    # Reload both. Source must be untouched; fork has new turns.
    src_re = await sm.get(src.id)
    fork_re = await sm.get(fork.id)
    assert src_re is not None and fork_re is not None
    assert len(src_re.messages) == 5  # original
    assert len(fork_re.messages) == 5  # 3 carried + 2 new
    last_fork_text = fork_re.messages[-1].content[0].text  # type: ignore[union-attr]
    assert last_fork_text == "postgres instead"
    last_src_text = src_re.messages[-1].content[0].text  # type: ignore[union-attr]
    assert last_src_text == "here's more detail"
    sm.close()


# ═══════════════════════════════════════════════════════════════════════
# Scenario 5: cross-session memory recall via SessionMemoryHook
# ═══════════════════════════════════════════════════════════════════════


async def test_scenario_memory_recall_across_sessions(tmp_path: Path) -> None:
    """Session A talks about something distinctive → archived → indexed.
    A new MemoryRetriever search returns the chunks back."""
    store = MemoryStore(tmp_path / "mem.db")
    embeds = StubEmbeddings()
    store.ensure_schema_meta(embeds.provider_name, embeds.model, embeds.dimensions)
    cache = EmbeddingCache(embeds, store)  # type: ignore[arg-type]
    hook = SessionMemoryHook(store=store, embeddings=cache, max_chars=300)

    sm = SQLiteSessionManager(tmp_path / "sessions.db")
    a = await sm.create(CreateAgentSessionOptions(agent_id="x", title="kraken-discussion"))
    a.messages = [
        SystemMessage(content="you are a marine biologist"),
        UserMessage(content="tell me about kraken locomotion in cold water"),
        AssistantMessage(
            content=[TextContent(text="krakens propel via mantle contraction")],
            stop_reason="end_turn",
        ),
        UserMessage(content="and their feeding behaviour?"),
        AssistantMessage(
            content=[TextContent(text="krakens hunt by ambush, using tentacles")],
            stop_reason="end_turn",
        ),
    ]
    await sm.save(a)
    n = await hook.index_session(a)
    assert n > 0

    # New session B: search memory for kraken.
    retriever = MemoryRetriever(
        store=store,
        embeddings_cache=cache,
        memory_dir=tmp_path / "memdir",
        inbox_path=tmp_path / "inbox.md",
    )
    hits = await retriever.search(query="kraken locomotion", k=3)
    assert hits, "memory recall returned no hits"
    texts = " ".join(h.chunk.text for h in hits)
    assert "kraken" in texts.lower()

    sm.close()


# ═══════════════════════════════════════════════════════════════════════
# Scenario 6: failover after persistent provider error
# ═══════════════════════════════════════════════════════════════════════


async def test_scenario_failover_after_persistent_provider_error(
    tmp_path: Path,
) -> None:
    """Primary model errors are classified as transient → retried; if the
    classifier sees a model_error/auth, the operator picks a failover."""
    primary = _model("scn6_primary", ctx=100_000)
    backup = _model("scn6_backup", ctx=80_000)

    state = {"calls": 0}

    async def primary_stream(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        # Three transient errors then we give up via max_retries.
        yield ErrorEvent(message="HTTP 503 Service Unavailable", retryable=True)

    async def backup_stream(ctx, opts):  # type: ignore[no-untyped-def]
        yield TextDeltaEvent(delta="failover answer")
        yield StopEvent(reason="end_turn")

    register_provider_stream("scn6_primary", primary_stream)
    register_provider_stream("scn6_backup", backup_stream)

    # Run an attempt with primary — exhausts retries then surfaces error.
    config = RuntimeConfig(max_retries=2, backoff_initial=0.0, backoff_max=0.0)
    result = await run_agent_turn(
        model=primary,
        api=type(
            "Api", (), {"base_url": "x", "api_key": None, "organization": None, "extra_headers": {}}
        )(),
        system="be brief",
        history=[UserMessage(content="hi")],
        tools=[],
        config=config,
    )
    assert result.stopped_reason == "error"
    # Classify the primary's error → transient (HTTP 503 matches).
    assert classify_failure(result.attempts[-1].error.message) == "transient"  # type: ignore[union-attr]

    # Operator picks a failover. Our pool has only the backup; it must be picked.
    pool = [primary, backup]
    pick = select_failover_model(primary, pool)
    assert pick is not None and pick.id == backup.id

    # Re-run with the failover model and verify it succeeds.
    result2 = await run_agent_turn(
        model=pick,
        api=type(
            "Api", (), {"base_url": "x", "api_key": None, "organization": None, "extra_headers": {}}
        )(),
        system="be brief",
        history=[UserMessage(content="hi")],
        tools=[],
        config=RuntimeConfig(max_retries=0),
    )
    assert result2.stopped_reason == "end_turn"
    assert "failover answer" in result2.final_message.content[0].text  # type: ignore[union-attr]


# ═══════════════════════════════════════════════════════════════════════
# Scenario 7: disk-budget prune keeps store under cap, retains keep_min
# ═══════════════════════════════════════════════════════════════════════


async def test_scenario_disk_budget_prunes_oldest_sessions(tmp_path: Path) -> None:
    sm = SQLiteSessionManager(tmp_path / "ops.db")

    # Seed many fat sessions to push past a tiny budget.
    for i in range(30):
        s = await sm.create(CreateAgentSessionOptions(agent_id="x", title=f"s{i}"))
        s.messages = [
            UserMessage(content=f"u{i} " + "x" * 4000),
            AssistantMessage(
                content=[TextContent(text=f"a{i} " + "y" * 4000)],
                stop_reason="end_turn",
            ),
        ]
        await sm.save(s)

    listed_before = await sm.list()
    pre_size = db_size_bytes(sm._path)
    assert len(listed_before) == 30
    assert pre_size > 0

    cfg = MaintenanceConfig(
        interval_seconds=1,
        max_age_seconds=None,
        max_sessions=None,
        max_disk_bytes=max(20_000, pre_size // 4),
        keep_min_per_agent=3,
    )
    mt = StoreMaintenance(sm, config=cfg)
    summary = await mt.tick()
    assert summary["by_disk"]["removed"] > 0

    listed_after = await sm.list()
    # keep_min preserved + many sessions removed (exact byte size depends on
    # sqlite WAL behaviour; we assert on logical session count instead).
    assert 3 <= len(listed_after) < len(listed_before)
    sm.close()


# ═══════════════════════════════════════════════════════════════════════
# Scenario 8: full session lifecycle via the CLI
# ═══════════════════════════════════════════════════════════════════════


def test_scenario_cli_full_session_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive `oxenclaw session ...` against a freshly-seeded store:
    list → show → fork → archive → restore → delete."""
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))

    async def _seed() -> str:
        sm = SQLiteSessionManager(tmp_path / "sessions.db")
        s = await sm.create(CreateAgentSessionOptions(agent_id="x", title="cli-flow"))
        s.messages = [
            SystemMessage(content="be helpful"),
            UserMessage(content="hello"),
            AssistantMessage(content=[TextContent(text="hi back")], stop_reason="end_turn"),
        ]
        await sm.save(s)
        sm.close()
        return s.id

    sid = asyncio.run(_seed())
    runner = CliRunner()

    # list
    res = runner.invoke(sessions_cli_app, ["list", "--json"])
    assert res.exit_code == 0
    rows = json.loads(res.stdout)
    assert any(r["title"] == "cli-flow" for r in rows)

    # show
    res = runner.invoke(sessions_cli_app, ["show", sid])
    assert res.exit_code == 0
    assert "hi back" in res.stdout

    # fork
    res = runner.invoke(sessions_cli_app, ["fork", sid])
    assert res.exit_code == 0
    assert "forked → " in res.stdout
    fork_id = res.stdout.split("forked → ")[1].split()[0]

    # archive (--keep so the row stays)
    res = runner.invoke(sessions_cli_app, ["archive", sid, "--keep"])
    assert res.exit_code == 0
    archive_path_str = res.stdout.strip().split("archived → ", 1)[1].split()[0]
    archive_path = Path(archive_path_str)
    assert archive_path.exists()

    # delete original
    res = runner.invoke(sessions_cli_app, ["delete", sid, "--yes"])
    assert res.exit_code == 0
    assert "deleted" in res.stdout

    # restore from archive (re-creates the original session id)
    res = runner.invoke(sessions_cli_app, ["restore", str(archive_path)])
    assert res.exit_code == 0
    assert "restored → " in res.stdout

    # Final list contains both fork + restored original.
    res = runner.invoke(sessions_cli_app, ["list", "--json"])
    rows = json.loads(res.stdout)
    ids = {r["id"] for r in rows}
    assert sid in ids
    assert fork_id in ids


# ═══════════════════════════════════════════════════════════════════════
# Scenario 9: PiAgent uses web_search → web_fetch chain end-to-end
# ═══════════════════════════════════════════════════════════════════════


async def test_scenario_pi_agent_web_search_then_fetch(tmp_path: Path) -> None:
    """A turn where the assistant calls web_search, gets URLs, then calls
    web_fetch on a target. Both tools are injected as fakes — the test
    asserts the run loop wires tool args/results across two iterations
    and that the SSRF guard fires on a private URL."""
    from oxenclaw.tools_pkg.web import (
        SearchHit,
        web_fetch_tool,
        web_search_tool,
    )

    class _P:
        name = "fake"

        async def search(self, query, *, k):  # type: ignore[no-untyped-def]
            return [SearchHit(title="Doc", url="https://example.org/a", snippet="x")]

    state = {"calls": 0}

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if state["calls"] == 1:
            yield ToolUseStartEvent(id="t1", name="web_search")
            yield ToolUseInputDeltaEvent(id="t1", input_delta='{"query":"langchain","k":3}')
            yield ToolUseEndEvent(id="t1")
            yield StopEvent(reason="tool_use")
        elif state["calls"] == 2:
            yield ToolUseStartEvent(id="t2", name="web_fetch")
            yield ToolUseInputDeltaEvent(
                id="t2",
                input_delta='{"url":"http://10.0.0.1/","readability":false,"max_bytes":1000}',
            )
            yield ToolUseEndEvent(id="t2")
            yield StopEvent(reason="tool_use")
        else:
            yield TextDeltaEvent(delta="all done")
            yield StopEvent(reason="end_turn")

    register_provider_stream("scn9_provider", fake_stream)

    tools = ToolRegistry()
    tools.register(web_search_tool(providers=[_P()]))
    tools.register(web_fetch_tool())

    agent = PiAgent(
        agent_id="t",
        model_id="m-scn9_provider",
        registry=_registry(_model("scn9_provider")),
        auth=_auth("scn9_provider"),
        sessions=SQLiteSessionManager(tmp_path / "s.db"),
        tools=tools,
        paths=_paths(tmp_path),
    )
    ctx = AgentContext(agent_id="t", session_key="ws")
    outs = [sp async for sp in agent.handle(_inbound("research it"), ctx)]
    assert outs and outs[0].text == "all done"
    [entry] = await agent._sessions.list(agent_id="t")
    fetched = await agent._sessions.get(entry.id)
    assert fetched is not None
    bodies: list[str] = []
    for m in fetched.messages:
        if m.role == "tool_result":
            for r in m.results:  # type: ignore[union-attr]
                if isinstance(r.content, str):
                    bodies.append(r.content)
    text = " ".join(bodies)
    # web_search returned the fake hit URL.
    assert "example.org/a" in text
    # web_fetch refused the SSRF target.
    assert "non-public" in text or "private" in text.lower()


# ═══════════════════════════════════════════════════════════════════════
# Scenario 10: skill runtime + coding_agent via fake CLI
# ═══════════════════════════════════════════════════════════════════════


async def test_scenario_coding_agent_runs_in_skill_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oxenclaw.clawhub.frontmatter import parse_skill_text
    from oxenclaw.clawhub.loader import InstalledSkill
    from oxenclaw.tools_pkg.coding import coding_agent_tool

    bd = tmp_path / "bin"
    bd.mkdir(parents=True)
    fake = bd / "codex"
    fake.write_text('#!/usr/bin/env bash\necho "PWD=$PWD"\necho "FLAG=$SKILL_FLAG"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bd}:/usr/bin:/bin")

    md = (
        "---\n"
        "name: coding-agent\n"
        "description: t\n"
        "openclaw:\n"
        "  workspace:\n"
        "    kind: ephemeral\n"
        "  env_overrides:\n"
        '    SKILL_FLAG: "green"\n'
        "---\n"
    )
    manifest, body = parse_skill_text(md)
    skill = InstalledSkill(
        slug="coding-agent",
        manifest=manifest,
        skill_md_path=Path("/tmp/coding-agent/SKILL.md"),
        body=body,
        origin=None,
    )
    paths = _paths(tmp_path)
    tool = coding_agent_tool(skill=skill, paths=paths)
    out = await tool.execute({"task": "demo"})
    assert "FLAG=green" in out
    assert "skill-workspaces" in out
    assert "coding_agent[codex] ok" in out


# ═══════════════════════════════════════════════════════════════════════
# Scenario 11: subagents tool — parent spawns child for sub-task
# ═══════════════════════════════════════════════════════════════════════


async def test_scenario_parent_spawns_subagent_and_summarises(
    tmp_path: Path,
) -> None:
    """Parent's stream issues a `subagents` tool call; the child runs one
    turn against its own model and returns text. Parent then wraps it."""
    from oxenclaw.tools_pkg.subagent import SubagentConfig, subagents_tool

    state = {"parent_calls": 0, "child_calls": 0}

    async def parent_stream(ctx, opts):  # type: ignore[no-untyped-def]
        state["parent_calls"] += 1
        if state["parent_calls"] == 1:
            yield ToolUseStartEvent(id="s1", name="subagents")
            yield ToolUseInputDeltaEvent(
                id="s1",
                input_delta='{"task":"summarise a paper","context":"ml"}',
            )
            yield ToolUseEndEvent(id="s1")
            yield StopEvent(reason="tool_use")
        else:
            yield TextDeltaEvent(delta="parent: child reported back")
            yield StopEvent(reason="end_turn")

    async def child_stream(ctx, opts):  # type: ignore[no-untyped-def]
        state["child_calls"] += 1
        first_user_text = ctx.messages[0].content
        assert "summarise a paper" in first_user_text
        assert "ml" in first_user_text
        yield TextDeltaEvent(delta="child summary: ok")
        yield StopEvent(reason="end_turn")

    register_provider_stream("scn11_parent", parent_stream)
    register_provider_stream("scn11_child", child_stream)

    parent_tools = ToolRegistry()
    parent_tools.register(
        subagents_tool(
            SubagentConfig(
                model=_model("scn11_child"),
                auth=InMemoryAuthStorage({"scn11_child": "k"}),  # type: ignore[dict-item]
                max_depth=2,
            )
        )
    )

    agent = PiAgent(
        agent_id="t",
        model_id="m-scn11_parent",
        registry=_registry(_model("scn11_parent"), _model("scn11_child")),
        auth=InMemoryAuthStorage(
            {"scn11_parent": "k", "scn11_child": "k"}  # type: ignore[dict-item]
        ),
        sessions=SQLiteSessionManager(tmp_path / "s.db"),
        tools=parent_tools,
        paths=_paths(tmp_path),
    )
    ctx = AgentContext(agent_id="t", session_key="dlg")
    outs = [sp async for sp in agent.handle(_inbound("delegate"), ctx)]
    assert outs and "child reported back" in outs[0].text
    assert state["child_calls"] == 1
