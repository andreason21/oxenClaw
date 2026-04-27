# Subagents Tool Audit: oxenClaw vs openclaw

Comparing `oxenclaw/tools_pkg/subagent.py` against
`openclaw/src/agents/tools/subagents-tool.ts`.

Audit date: 2026-04-27

---

## Five-Point Parity Table

| Point | openclaw `subagents-tool.ts` | oxenClaw `subagent.py` | Status |
|---|---|---|---|
| **Tool name** | `"subagents"` (single tool, action-dispatched: list/kill/steer) | `"subagents"` (single tool, async handler) | ◐ |
| **Args / schema** | `{ action?, target?, message?, recentMinutes? }` — orchestration control surface (list running runs, kill, steer mid-run) | `{ task: str, context?: str }` — task delegation, spawn a one-shot child | ✗ |
| **Execution model** | Sync-ish dispatch to `listControlledSubagentRuns`, `killControlledSubagentRun`, `steerControlledSubagentRun`; does NOT spawn a new process | Async: builds isolated `PiAgent` child, calls `run_agent_turn`, returns final text | ✗ |
| **Return shape** | `{ status, action, requesterSessionKey, active[], recent[], text, ... }` — JSON summary of live subagent registry | Plain `str` (child's final text, or error prefix) | ✗ |
| **Error path** | Structured `{ status: "error"|"forbidden", error: "..." }` JSON; forbidden actions surface separately from runtime errors | `try/except` → `"subagents: child failed: {exc}"` string prefix; recursion guard returns a prefixed string | ◐ |

Legend: ✓ = matches, ◐ = partial/structural match, ✗ = different.

---

## Detailed Findings

### 1. Tool name
Both expose a tool named `"subagents"`. ◐ — same surface name; completely different semantics.

### 2. Args (parent context bag, prompt, return contract)
openclaw's `subagents` is an **orchestration probe**: it lets a running agent inspect
or terminate *already-running* sibling subagent processes. It carries no task prompt
and produces no child output.

oxenClaw's `subagents` is a **task delegator**: it accepts a `task` string (+ optional
`context`), spins up a new isolated `PiAgent`, runs one full turn, and returns the
child's text. This maps more closely to openclaw's `sessions_spawn` + immediate
execution pattern, not to the `subagents` list/kill/steer surface.

### 3. Execution model (sync/async, isolation)
openclaw does not spawn new agents from the `subagents` tool; spawning is done by
`sessions_spawn`. The `subagents` tool is a read/control surface over an external
subprocess registry (`listControlledSubagentRuns`).

oxenClaw spawns inline (no subprocess, no external registry): an `InMemorySessionManager`
is created per call, `run_agent_turn` is `await`ed in the same event loop, recursion is
capped by `current_depth`. No inter-process isolation; the child shares the Python
process and event loop with the parent.

### 4. Return shape
openclaw returns structured JSON with `status`, `action`, and session-registry fields
that allow the model to reason about which subagents are active, their session keys,
and whether a kill/steer succeeded.

oxenClaw returns a bare `str` (the child's final reply). Callers cannot distinguish
a normal return from a depth-refused return without text-parsing the prefix.

### 5. Error path
openclaw uses `{ status: "error" | "forbidden" }` for all failure modes, keeping the
tool schema consistent.

oxenClaw raises (propagated to caller), catches with a blanket `except Exception`, and
returns a prefixed error string. The recursion guard also returns a prefixed string.
Neither path surfaces as structured JSON.

---

## Parity Verdict

**~20% parity** (tool name only; all semantic points diverge).

oxenClaw's `subagent.py` is a clean task-delegation primitive that does not exist in
openclaw under this name. openclaw's `subagents` tool is an orchestration *control*
surface. Both are useful; they address different concerns.

---

## Next-Session Tasks (gaps to close)

1. **Add oxenClaw orchestration tool** — implement a `SubagentControlTool` (or rename
   `subagents` to `spawn_subagent`) that controls already-running sub-tasks via a
   lightweight registry (session id → asyncio.Task). Parity target: list/kill actions.

2. **Structured error returns** — change `subagent.py` handler to return a JSON string
   `{"error": "...", "code": "depth_exceeded"|"child_failed"}` instead of plain-text
   prefixes. Enables dashboard tool-call cards to pretty-print failures.

3. **Subprocess isolation option** — for long-running child tasks, add an optional
   `isolated=True` mode that runs the child in a separate process (via `asyncio.create_subprocess_exec`
   or `concurrent.futures.ProcessPoolExecutor`) to match openclaw's OS-level isolation.

4. **Recursion registry** — persist active-depth state in a `SessionManager` metadata key
   so depth is enforced across process restarts, not just within a single Python session.

5. **`sessions_spawn` + `sessions_yield` as separate tools** — oxenClaw now ships these
   in `session_tools.py`; wire them into the `PiAgent` default tool set when a
   `SessionManager` is available (the factory hook for this was added in this session).
