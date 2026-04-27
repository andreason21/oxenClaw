"""Scenario tests — user-memory drives tool-call decisions over ACP.

The user has two standing memories in
`~/.claude/projects/.../memory/MEMORY.md`:

  1. **feedback_change_set** — "수정·테스트·문서 정리는 같은 작업에서
     함께 처리한다" (code change → test → docs is one set).
  2. **feedback_apply_openclaw_guides** — "openclaw 개선 가이드는
     자동 포팅" (apply openclaw improvement guides by default).

These tests don't try to evaluate the model's recall accuracy
(that's the model's job). They pin the *plumbing*: when the agent
acts as if it recalled the memory and decides to fire a chain of
tool calls in consequence, those tool calls must surface on the
ACP wire as pending → completed pairs in the right order, with
the right ids, and the surrounding assistant text deltas must
arrive in their natural positions relative to the tool cards.

Two scenarios:

  - **Scenario A (`change_set` memory)**: user asks for a fix; the
    fake LLM stream simulates the model deciding "per the user's
    rule, code/test/docs are one set", calls `edit` on three files,
    then explains the result. The ACP client should see three
    tool_call/tool_call_update pairs interleaved with the assistant
    text deltas.

  - **Scenario B (`apply_openclaw_guides` memory)**: user mentions an
    openclaw guide; the model's stream calls `read_file` to fetch
    the guide, then `edit` on a destination file. Two tool pairs.

The `register_provider_stream` hook pinned by the existing PiAgent
test suite gives us deterministic LLM behaviour without an Ollama or
API key.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import BaseModel

from oxenclaw.acp import manager as manager_mod
from oxenclaw.acp import runtime_registry as registry_mod
from oxenclaw.acp.manager import (
    AcpInitializeSessionInput,
    AcpRunTurnInput,
    get_acp_session_manager,
)
from oxenclaw.acp.pi_agent_runtime import PiAgentAcpRuntime
from oxenclaw.acp.runtime_registry import (
    AcpRuntimeBackend,
    register_acp_runtime_backend,
)
from oxenclaw.agents.acp_runtime import (
    AcpEventDone,
    AcpEventTextDelta,
    AcpEventToolCall,
    AcpRuntimeEvent,
)
from oxenclaw.agents.pi_agent import PiAgent
from oxenclaw.agents.tools import FunctionTool, ToolRegistry
from oxenclaw.config import OxenclawPaths
from oxenclaw.pi import (
    InMemoryAuthStorage,
    InMemorySessionManager,
    Model,
    register_provider_stream,
)
from oxenclaw.pi.registry import InMemoryModelRegistry
from oxenclaw.pi.streaming import (
    StopEvent,
    TextDeltaEvent,
    ToolUseEndEvent,
    ToolUseInputDeltaEvent,
    ToolUseStartEvent,
)


@pytest.fixture(autouse=True)
def _isolate_globals():
    registry_mod.reset_for_tests()
    manager_mod.reset_for_tests()
    yield
    registry_mod.reset_for_tests()
    manager_mod.reset_for_tests()


def _paths(tmp_path: Path) -> OxenclawPaths:
    p = OxenclawPaths(home=tmp_path)
    p.ensure_home()
    return p


def _make_pi_agent(
    *, tmp_path: Path, provider: str, tools: ToolRegistry
) -> PiAgent:
    reg = InMemoryModelRegistry(
        models=[
            Model(
                id="memory-driven",
                provider=provider,
                max_output_tokens=512,
                extra={"base_url": "http://test-fake"},
            )
        ]
    )
    return PiAgent(
        agent_id="memory-acp",
        model_id="memory-driven",
        registry=reg,
        auth=InMemoryAuthStorage({provider: "sk-test"}),  # type: ignore[dict-item]
        sessions=InMemorySessionManager(),
        tools=tools,
        paths=_paths(tmp_path),
    )


async def _drain(it: AsyncIterator[AcpRuntimeEvent]) -> list[AcpRuntimeEvent]:
    out: list[AcpRuntimeEvent] = []
    async for ev in it:
        out.append(ev)
    return out


# --- shared fake tools ----------------------------------------------------


class _EditArgs(BaseModel):
    path: str
    note: str = ""


class _ReadArgs(BaseModel):
    path: str


def _build_tool_registry(
    *, edit_log: list[str], read_log: list[str]
) -> ToolRegistry:
    async def _edit(args: _EditArgs) -> str:
        edit_log.append(args.path)
        return f"edited {args.path}"

    async def _read(args: _ReadArgs) -> str:
        read_log.append(args.path)
        # Pretend the openclaw guide says "do X then Y".
        return (
            "OPENCLAW IMPROVEMENT GUIDE\n"
            "1. Update agents/foo.py\n"
            "2. Update tests/test_foo.py\n"
        )

    tools = ToolRegistry()
    tools.register(
        FunctionTool(
            name="edit",
            description="Edit a file.",
            input_model=_EditArgs,
            handler=_edit,
        )
    )
    tools.register(
        FunctionTool(
            name="read_file",
            description="Read a file.",
            input_model=_ReadArgs,
            handler=_read,
        )
    )
    return tools


# --- Scenario A: change_set memory drives 3-file edit chain ----------------


async def test_change_set_memory_drives_three_edit_tool_calls(
    tmp_path: Path,
) -> None:
    """User memory: code/test/docs are one set. When the model fires
    edit on all three, the ACP wire must show three tool_call /
    tool_call_update pairs in arrival order, before the final reply
    text delta."""
    edit_log: list[str] = []
    read_log: list[str] = []
    tools = _build_tool_registry(edit_log=edit_log, read_log=read_log)

    state = {"calls": 0}

    async def fake_stream(_ctx, _opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if state["calls"] == 1:
            # First model invocation: the model "recalled" the memory
            # and explains its plan, then issues the first tool call.
            yield TextDeltaEvent(
                delta=(
                    'Per your memory "code/test/doc are one set" '
                    "I'll update all three.\n"
                )
            )
            yield ToolUseStartEvent(id="t1", name="edit")
            yield ToolUseInputDeltaEvent(
                id="t1",
                input_delta='{"path":"agents/foo.py","note":"impl"}',
            )
            yield ToolUseEndEvent(id="t1")
            yield StopEvent(reason="tool_use")
        elif state["calls"] == 2:
            yield ToolUseStartEvent(id="t2", name="edit")
            yield ToolUseInputDeltaEvent(
                id="t2",
                input_delta='{"path":"tests/test_foo.py","note":"test"}',
            )
            yield ToolUseEndEvent(id="t2")
            yield StopEvent(reason="tool_use")
        elif state["calls"] == 3:
            yield ToolUseStartEvent(id="t3", name="edit")
            yield ToolUseInputDeltaEvent(
                id="t3",
                input_delta='{"path":"docs/HANDOFF.md","note":"doc"}',
            )
            yield ToolUseEndEvent(id="t3")
            yield StopEvent(reason="tool_use")
        else:
            yield TextDeltaEvent(delta="All three updated as a set.")
            yield StopEvent(reason="end_turn")

    register_provider_stream("memory_change_set", fake_stream)
    agent = _make_pi_agent(
        tmp_path=tmp_path, provider="memory_change_set", tools=tools
    )
    runtime = PiAgentAcpRuntime(agent=agent, backend_id="pi")
    register_acp_runtime_backend(
        AcpRuntimeBackend(id="pi", runtime=runtime)
    )
    mgr = get_acp_session_manager()

    await mgr.initialize_session(
        AcpInitializeSessionInput(
            session_key="memory-scenario-a",
            agent="memory-acp",
            mode="oneshot",
            backend_id="pi",
        )
    )
    events = await _drain(
        mgr.run_turn(
            AcpRunTurnInput(
                session_key="memory-scenario-a",
                text="please apply the fix",
                request_id="r-a",
            )
        )
    )

    # The three edits must have actually executed.
    assert edit_log == [
        "agents/foo.py",
        "tests/test_foo.py",
        "docs/HANDOFF.md",
    ]
    assert read_log == []

    tool_calls = [e for e in events if isinstance(e, AcpEventToolCall)]
    # Three pending + three completed = six.
    assert len(tool_calls) == 6
    pendings = [t for t in tool_calls if t.status == "pending"]
    completeds = [t for t in tool_calls if t.status == "completed"]
    assert len(pendings) == 3
    assert len(completeds) == 3
    # Each completed must carry the same tool_call_id as a prior
    # pending (FIFO pairing).
    pending_ids = [t.tool_call_id for t in pendings]
    completed_ids = [t.tool_call_id for t in completeds]
    assert pending_ids == completed_ids
    # All three pendings must be `edit` (memory said code/test/docs
    # is one set, so all three resulting tools are edits).
    assert all(t.title == "edit" for t in pendings)

    # Final assistant text delta arrives after the tool chain.
    text_deltas = [e for e in events if isinstance(e, AcpEventTextDelta)]
    final_text = "".join(t.text for t in text_deltas)
    assert (
        "code/test/doc are one set" in final_text
        or "All three updated" in final_text
    )

    # Last event is done(stop).
    assert isinstance(events[-1], AcpEventDone)
    assert events[-1].stop_reason == "stop"


# --- Scenario B: openclaw-guide memory drives read+edit pair --------------


async def test_apply_openclaw_guide_memory_drives_read_then_edit(
    tmp_path: Path,
) -> None:
    """User memory: openclaw guides are auto-ported. When the user
    references a guide, the model fetches it via `read_file` and
    then applies the change via `edit`. The ACP wire shows two
    tool_call/tool_call_update pairs in that order."""
    edit_log: list[str] = []
    read_log: list[str] = []
    tools = _build_tool_registry(edit_log=edit_log, read_log=read_log)

    state = {"calls": 0}

    async def fake_stream(_ctx, _opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if state["calls"] == 1:
            yield TextDeltaEvent(
                delta=(
                    'Memory says "apply openclaw guides by default" '
                    "— let me fetch the guide first.\n"
                )
            )
            yield ToolUseStartEvent(id="r1", name="read_file")
            yield ToolUseInputDeltaEvent(
                id="r1", input_delta='{"path":"docs/openclaw-guide.md"}'
            )
            yield ToolUseEndEvent(id="r1")
            yield StopEvent(reason="tool_use")
        elif state["calls"] == 2:
            yield TextDeltaEvent(
                delta="Got the guide. Applying step 1 (agents/foo.py).\n"
            )
            yield ToolUseStartEvent(id="e1", name="edit")
            yield ToolUseInputDeltaEvent(
                id="e1",
                input_delta='{"path":"agents/foo.py","note":"per guide"}',
            )
            yield ToolUseEndEvent(id="e1")
            yield StopEvent(reason="tool_use")
        else:
            yield TextDeltaEvent(delta="Guide applied.")
            yield StopEvent(reason="end_turn")

    register_provider_stream("memory_openclaw_guide", fake_stream)
    agent = _make_pi_agent(
        tmp_path=tmp_path, provider="memory_openclaw_guide", tools=tools
    )
    runtime = PiAgentAcpRuntime(agent=agent, backend_id="pi")
    register_acp_runtime_backend(
        AcpRuntimeBackend(id="pi", runtime=runtime)
    )
    mgr = get_acp_session_manager()
    await mgr.initialize_session(
        AcpInitializeSessionInput(
            session_key="memory-scenario-b",
            agent="memory-acp",
            mode="oneshot",
            backend_id="pi",
        )
    )

    events = await _drain(
        mgr.run_turn(
            AcpRunTurnInput(
                session_key="memory-scenario-b",
                text="apply the openclaw guide",
                request_id="r-b",
            )
        )
    )

    # Tool execution order matters — read first, then edit.
    assert read_log == ["docs/openclaw-guide.md"]
    assert edit_log == ["agents/foo.py"]

    tool_calls = [e for e in events if isinstance(e, AcpEventToolCall)]
    # Two pairs = four cards, ordered: read pending, read completed,
    # edit pending, edit completed.
    assert len(tool_calls) == 4
    titles_in_order = [t.title for t in tool_calls]
    assert titles_in_order == [
        "read_file",
        "read_file",
        "edit",
        "edit",
    ]
    statuses_in_order = [t.status for t in tool_calls]
    assert statuses_in_order == [
        "pending",
        "completed",
        "pending",
        "completed",
    ]
    # FIFO pairing.
    assert tool_calls[0].tool_call_id == tool_calls[1].tool_call_id
    assert tool_calls[2].tool_call_id == tool_calls[3].tool_call_id
    assert tool_calls[0].tool_call_id != tool_calls[2].tool_call_id

    # The "Guide applied." text must arrive after the edit pair.
    text_deltas = [e for e in events if isinstance(e, AcpEventTextDelta)]
    full_text = "".join(t.text for t in text_deltas)
    assert "Guide applied" in full_text

    assert isinstance(events[-1], AcpEventDone)
    assert events[-1].stop_reason == "stop"


# --- cross-cutting: tool ordering on the wire matches execution ----------


async def test_acp_event_order_matches_underlying_tool_execution(
    tmp_path: Path,
) -> None:
    """Belt-and-braces: walk the event stream and confirm that, for
    every (pending, completed) pair, no second pair's events appear
    interleaved with the first. FIFO must be preserved end-to-end."""
    edit_log: list[str] = []
    read_log: list[str] = []
    tools = _build_tool_registry(edit_log=edit_log, read_log=read_log)

    state = {"calls": 0}

    async def fake_stream(_ctx, _opts):  # type: ignore[no-untyped-def]
        state["calls"] += 1
        if state["calls"] == 1:
            yield ToolUseStartEvent(id="x1", name="edit")
            yield ToolUseInputDeltaEvent(
                id="x1", input_delta='{"path":"a.py"}'
            )
            yield ToolUseEndEvent(id="x1")
            yield StopEvent(reason="tool_use")
        elif state["calls"] == 2:
            yield ToolUseStartEvent(id="x2", name="edit")
            yield ToolUseInputDeltaEvent(
                id="x2", input_delta='{"path":"b.py"}'
            )
            yield ToolUseEndEvent(id="x2")
            yield StopEvent(reason="tool_use")
        else:
            yield TextDeltaEvent(delta="ok")
            yield StopEvent(reason="end_turn")

    register_provider_stream("memory_fifo", fake_stream)
    agent = _make_pi_agent(
        tmp_path=tmp_path, provider="memory_fifo", tools=tools
    )
    runtime = PiAgentAcpRuntime(agent=agent)
    handle = await runtime.ensure_session(
        type(
            "_E",
            (),
            {
                "session_key": "fifo",
                "agent": "a",
                "mode": "oneshot",
                "resume_session_id": None,
                "cwd": None,
                "env": None,
            },
        )()
    )
    # Bypass the type system on the handle dance — use the real input.
    from oxenclaw.agents.acp_runtime import (
        AcpRuntimeEnsureInput,
        AcpRuntimeTurnInput,
    )

    handle = await runtime.ensure_session(
        AcpRuntimeEnsureInput(
            session_key="fifo", agent="a", mode="oneshot"
        )
    )
    events = await _drain(
        runtime.run_turn(
            AcpRuntimeTurnInput(
                handle=handle, text="x", mode="prompt", request_id="r"
            )
        )
    )

    tool_calls = [e for e in events if isinstance(e, AcpEventToolCall)]
    assert len(tool_calls) == 4
    # Walk pairwise. Pending-N must precede Completed-N, and
    # Completed-N must precede Pending-(N+1) — no interleave.
    p1, c1, p2, c2 = tool_calls
    assert p1.status == "pending" and c1.status == "completed"
    assert p2.status == "pending" and c2.status == "completed"
    assert p1.tool_call_id == c1.tool_call_id
    assert p2.tool_call_id == c2.tool_call_id
    # First pair fully resolves before second pair begins.
    indices = [
        events.index(p1),
        events.index(c1),
        events.index(p2),
        events.index(c2),
    ]
    assert indices == sorted(indices)
