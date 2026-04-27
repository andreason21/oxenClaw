# ACP — Agent Client Protocol

oxenClaw speaks **ACP** (Agent Client Protocol — JSON-RPC over
NDJSON, defined by the Zed-led `@agentclientprotocol/sdk` 0.19.x)
in **both directions**:

- as an **agent** that an external ACP client (Zed, another
  oxenclaw, or any conforming peer) can spawn over stdio —
  `oxenclaw acp [--backend fake|pi]`;
- as a **client** that spawns and drives a child ACP server —
  `oxenclaw.acp.subprocess_runtime.SubprocessAcpRuntime`.

This page is the canonical reference. README only carries the
five-line orientation; the install / usage / scenario detail
lives here.

---

## Package layout

```
oxenclaw/acp/
├── framing.py             — NDJSON reader/writer (read_messages, write_message)
├── protocol.py            — pydantic models for the four foundational verbs
│                            (initialize / session/new / session/prompt /
│                            session/cancel) + JSON-RPC envelope helpers.
│                            PROTOCOL_VERSION pinned at 0.19.0.
├── manager.py             — AcpSessionManager singleton + AcpInitializeSessionInput /
│                            AcpRunTurnInput / AcpCloseSessionInput. Routes to a
│                            registered AcpRuntime backend.
├── runtime_registry.py    — register_acp_runtime_backend(id, runtime),
│                            get_acp_runtime_backend(id?), require_*().
├── fake_runtime.py        — InMemoryFakeRuntime (echo backend, used by
│                            `oxenclaw acp --backend fake` and tests).
├── subprocess_runtime.py  — SubprocessAcpRuntime — real NDJSON wire client.
│                            One backend instance owns one child process,
│                            serves N ACP sessions over the same wire.
├── pi_agent_runtime.py    — PiAgentAcpRuntime — wrap a PiAgent so an ACP
│                            client gets real LLM streaming + tool execution.
│                            Tool-call telemetry projected mid-flight via
│                            HookRunner before/after_tool_use hooks.
└── server.py              — AcpServer + `python -m oxenclaw.acp.server` CLI
                              (also reachable as `oxenclaw acp`).

oxenclaw/agents/
├── acp_runtime.py         — Protocol stub: AcpRuntime (required surface) +
│                            AcpRuntimeOptional, dataclass event union
│                            (text_delta / status / tool_call / done / error).
├── acp_subprocess.py      — Legacy one-shot CLI shell-out. Predates the
│                            real wire; kept for the `sessions_spawn` tool.
└── acp_parent_stream.py   — Operator-visible relay: JSONL audit log
                              + 60s stall watchdog + 6h lifetime cap.
                              Mirrors openclaw acp-spawn-parent-stream.ts.
```

---

## Lifecycle (the four foundational verbs)

```
client                                              agent (oxenclaw)
  │ ─── initialize {protocolVersion, clientInfo} ──> │
  │ <── {protocolVersion, agentInfo}              ── │
  │                                                  │
  │ ─── session/new {_meta.sessionKey?}           ─> │
  │ <── {sessionId}                               ── │
  │                                                  │
  │ ─── session/prompt {sessionId, prompt:[...]}  ─> │
  │ <── session/update {agent_message_chunk}      ── │   (zero or more)
  │ <── session/update {tool_call pending}        ── │
  │ <── session/update {tool_call_update done}    ── │
  │ <── session/update {agent_message_chunk}      ── │
  │ <── {stopReason: "stop"|"cancel"|"error"}     ── │
  │                                                  │
  │ ─── session/cancel {sessionId}                ─> │   (any time)
```

Wire format: NDJSON over stdio, one JSON-RPC envelope per line,
UTF-8, `\n`-terminated. Logs go to stderr only; stdout is reserved
for protocol traffic.

---

## Run oxenclaw as an ACP agent

The lowest-friction entry point:

```bash
oxenclaw acp --backend pi
```

Reads NDJSON JSON-RPC from stdin, writes responses + `session/update`
notifications to stdout. Two backends ship in core:

| `--backend` | what it is | when to use |
|---|---|---|
| `fake` (default) | `InMemoryFakeRuntime` — echoes the prompt as one `text_delta` then `done`. No LLM. | Smoke tests, doctor-style probes, IDE wiring sanity check. |
| `pi` | `PiAgentAcpRuntime` wrapping a real `PiAgent` (Ollama / `gemma4:latest` by default). Memory tools (`memory_save` / `memory_search` / `memory_get`) auto-registered, recall prelude path live, tool-call telemetry projected mid-flight. | Production. What an IDE-side ACP integration actually wants. |

The `pi` backend reads the user's standard `~/.oxenclaw/` paths and
builds a `MemoryRetriever` against the default Ollama embedder
(`OLLAMA_HOST` env var honoured). If the embedder cannot be reached
the agent still boots — without memory tools rather than crashing.

### Connect Zed (or any ACP client)

Zed reads `~/.config/zed/agent_servers.json`. Add an entry:

```json
{
  "oxenclaw": {
    "command": "oxenclaw",
    "args": ["acp", "--backend", "pi"]
  }
}
```

Open the Agent panel, pick `oxenclaw`, type a prompt. You'll see:

- assistant text streaming as `agent_message_chunk` notifications;
- live tool-call cards (PiAgent's `read_file` / `edit` / `grep` /
  `memory_search` / etc.) as `tool_call` → `tool_call_update`
  pairs with consistent `toolCallId`;
- stop reason (`stop` / `cancel` / `error`) on prompt completion.

---

## Drive a child ACP server from oxenclaw

The inverse direction. `SubprocessAcpRuntime` spawns a child that
speaks ACP and routes its `session/update` notifications back as
`AcpRuntimeEvent` instances:

```python
from oxenclaw.acp.subprocess_runtime import SubprocessAcpRuntime
from oxenclaw.acp.runtime_registry import register_acp_runtime_backend, AcpRuntimeBackend
from oxenclaw.acp.manager import (
    get_acp_session_manager,
    AcpInitializeSessionInput,
    AcpRunTurnInput,
)

rt = SubprocessAcpRuntime(argv=["claude", "acp"], backend_id="claude-code")
register_acp_runtime_backend(AcpRuntimeBackend(id="claude-code", runtime=rt))

mgr = get_acp_session_manager()
handle = await mgr.initialize_session(
    AcpInitializeSessionInput(
        session_key="ide-claude:1",
        agent="claude-code",
        mode="oneshot",
        backend_id="claude-code",
    )
)
async for ev in mgr.run_turn(
    AcpRunTurnInput(
        session_key="ide-claude:1",
        text="refactor foo.py",
        request_id="r-1",
    )
):
    print(ev)  # AcpEventTextDelta / AcpEventToolCall / AcpEventDone

await rt.aclose()
```

One backend instance owns one child process and can serve N ACP
sessions over the same wire. `aclose()` is idempotent; pending
requests resolve to `AcpWireError(-32001)` if the child shuts
down before they complete.

---

## Worked scenario: "나는 수원 살아" → "내가 사는 곳 날씨 알려줘"

The end-to-end memory-driven tool-call flow that exercises every
layer at once:

| step | wire event | agent action | memory state |
|---|---|---|---|
| **Turn 1** user says `"나는 수원 살아"` | `session/prompt` | model fires `memory_save("User lives in Suwon, South Korea …")` | inbox.md gains the chunk; index re-built |
| (turn 1 cont.) | `session/update {tool_call: memory_save, pending}` | tool starts | — |
| (turn 1 cont.) | `session/update {tool_call_update: memory_save, completed}` | tool returns | chunk is now searchable |
| (turn 1 cont.) | `session/update {agent_message_chunk}` then `{stopReason:"stop"}` | "기억해 둘게요." | — |
| **Turn 2** user says `"내가 사는 곳 날씨 알려줘"` | `session/prompt` | PiAgent's `_build_user_recall_prelude` runs, prepends a `<recalled_memories>` block carrying "User lives in Suwon" to the user message before the model sees it | — |
| (turn 2 cont.) | model resolves the deictic phrase using the prelude → fires `weather(location="Suwon")` | tool runs | — |
| (turn 2 cont.) | `session/update {tool_call: weather, pending}` → `{tool_call_update: weather, completed}` | — | — |
| (turn 2 cont.) | `session/update {agent_message_chunk}` then `{stopReason:"stop"}` | "수원은 현재 맑고 20도입니다." | — |

Tests in
[`tests/test_acp_two_turn_memory_disambig.py`](../tests/test_acp_two_turn_memory_disambig.py)
pin every layer of this flow. The fake LLM stream's tool-arg
decision in turn 2 is **conditioned on what it actually reads in
the user message** — if the recall prelude is missing, the tool
gets called with the literal deictic phrase, NOT "Suwon", and the
test fails with a clear diagnostic.

---

## Operator-visible observability

`AcpParentStreamRelay` (see `oxenclaw/agents/acp_parent_stream.py`)
gives the parent session a JSONL audit log + a 60-second stall
watchdog whenever oxenclaw is *driving* a long-running ACP child
(via `SubprocessAcpRuntime`). Default location:

```
<sessionFile>.acp-stream.jsonl   (mode 0o600)
```

Each line is a JSON object with `ts`, `epoch_ms`, `run_id`,
`parent_session_key`, `child_session_key`, `agent_id`, `kind`
(`system_event` / `end`), and per-event fields.

Stall watchdog: when a 60-second window passes without progress,
the relay emits a system event "child has produced no output for
60s. It may be waiting for interactive input." The cap, the
maximum lifetime (6h), and the coalesce window (2.5s) all match
openclaw's defaults from `acp-spawn-parent-stream.ts`.

---

## What's not yet implemented

| Feature | Status |
|---|---|
| Capability negotiation in `InitializeResult.capabilities` | TODO — server returns basic `agentInfo` only |
| `setMode` / `setConfigOption` round-trips | TODO |
| Image / resource content blocks in `prompt[]` | TODO — text-only for now |
| `session/load` (resume by `sessionId`) | TODO |
| Plan / usage projection | TODO |
| File / terminal client-initiated methods | Out of scope (mirrors openclaw decision) |

---

## See also

- [`tests/test_acp_*.py`](../tests/) — 84 tests across framing,
  protocol, manager, registry, fake runtime, subprocess runtime,
  parent stream, server (loopback E2E), PiAgent adapter,
  tool-call telemetry, scenario tests.
- [`oxenclaw/agents/acp_runtime.py`](../oxenclaw/agents/acp_runtime.py)
  — Python `Protocol` mirror of openclaw's
  `src/acp/runtime/types.ts`.
- openclaw's `docs.acp.md` (in the upstream repo) — the canonical
  protocol spec we track.
