# oxenClaw → openclaw parity roadmap

Live working list: gaps from the openclaw comparison + the operator's
explicit asks. Order is by user-visible impact and parallelisability.
Each task is self-contained: code change + test + doc update is one
set per the project's standing rule (see `feedback_change_set` memory).

Legend: P0 = ship now • P1 = next sprint • P2 = larger effort • ✗ open • ◐ in-flight • ✓ done

---

## P0 — operator-named, small to medium

### A. "New chat" entry in the dashboard ✓
**Shipped** — `+ New chat` button in chat-tab topbar, Ctrl+Shift+N
shortcut, "Start a new chat" command-palette entry. New chat-id
format `chat-YYYYMMDDHHMMSS-XXXX`. PiAgent creates the matching
ConversationHistory lazily on first send. Dashboard E2E test
`test_chat_view_new_chat_button_assigns_fresh_chat_id`.

### A-historical plan (kept for context)
- **Why**: every fresh conversation should start a new session; the
  dashboard's only knob today is editing `chat_id` by hand. The
  operator listed this as a top requirement.
- **Plan**:
  - Add a `+ New chat` button in the chat tab's session-panel header.
  - Click → generate a fresh chat-id (timestamp + 4 hex chars), reset
    `ChatState.chatId`, persist localStorage, refresh the chat pane.
  - Optional: also expose Ctrl+N as a keyboard shortcut + add a
    "New chat" entry to the command palette.
  - Tests: confirm `ChatState.save` persists the new chat-id,
    `chat.history` for the new key returns 0 messages.
- **Files**: `oxenclaw/static/app.js` only. No backend RPC needed —
  PiAgent's `_ensure_session` already creates on first chat.send.

### B. Tool-call telemetry surfaced in chat ✓
**Shipped**.

  - **Backend**: PiAgent now persists `tool_calls` per assistant turn
    in ConversationHistory with `{id, name, args, started_at,
    ended_at, status, output_preview}`. Timing comes from
    `ToolExecutionResult.duration_seconds` accumulated against the
    turn's wall-clock start.
  - **Frontend**: chat stream renders an expandable `<details>` card
    per tool call — tool name + elapsed (ms or s) + status icon (✓/⚠).
    Click to expand args + output preview. CSS in `app.css`
    `.tool-call-card`.
  - **Tests**: `test_pi_agent_records_tool_call_timing` (backend
    contract), `test_chat_view_tool_call_card_renders_with_elapsed`
    (DOM shape).

### B-historical plan (kept for context)
- **Why**: openclaw's `<agent_event>` stream pushes
  `item:start/update/end` with `kind`, `name`, `startedAt`, `endedAt`,
  `status`, `summary`. Dashboards render tool-call cards inline in
  the assistant message. We currently show nothing.
- **Plan**:
  - **Backend (B-1)**: `pi_agent.py` already runs tools through
    `oxenclaw/pi/run/run.py`. Tap before/after-tool hooks to
    persist `(tool_name, args_summary, started_at, ended_at, status,
    output_summary)` into the `ConversationHistory` `tool_call_meta`
    field, alongside the existing `tool_calls` list. The dashboard
    reads chat.history so this lands without a new RPC.
  - **Frontend (B-2)**: render tool-call cards inline in the
    assistant bubble — title, ⚡ icon, elapsed ms, expandable args/
    output. Reuse openclaw's `tool-cards.ts` visual idiom.
  - Tests: regression that a multi-tool turn populates `tool_calls`
    with timing fields; visual snapshot in the dashboard E2E suite
    if practical.
- **Files**: `oxenclaw/agents/pi_agent.py`, `oxenclaw/agents/history.py`,
  `oxenclaw/static/app.js`, `oxenclaw/static/app.css`.

---

## P1 — operator-named, medium

### C. Skill discovery → install → invoke pipeline ✗
- **Why**: operator wants the agent to recognise an intent, locate a
  matching skill in clawhub, install it, and execute it without the
  user manually running `skills.search` + `skills.install`.
- **Plan**:
  - Surface a new pi-runtime tool `skill_resolver(query)` that:
    1. Calls `skills.search` against the configured registries.
    2. Picks the top match (by name match + description fuzzy score).
    3. If not installed → `skills.install`.
    4. Reads `<location>/SKILL.md` and the scripts dir, returns a
       short instruction block the LLM can act on (paths + commands).
  - System-prompt note: "if a request mentions a domain you have no
    skill for, you may call `skill_resolver` to fetch one before
    refusing."
  - Tests: end-to-end with a fake registry that ships a single
    `weather` skill — `skill_resolver("what's the weather")` returns
    the install-then-instruction payload.
- **Files**: new `oxenclaw/tools_pkg/skill_resolver_tool.py`,
  edits to `oxenclaw/agents/pi_agent.py` (register it),
  `oxenclaw/clawhub/loader.py` (system-prompt copy).

### D. `/new` slash command + session list "new" affordance ✗
- Pair with task A. Once the chat tab has a button, also wire `/new`
  in the compose box and a "New chat" row at the top of the sessions
  panel.

### E. Chat HEAD support on `/healthz` ✗
- Five-line gateway fix: add HEAD handler so external monitors that
  default to HEAD (k8s, datadog) work without falling back to GET.

---

## P2 — large efforts

### F. Pi-coding-agent equivalent ✗
- **Why**: openclaw ships a specialised coding agent
  (`pi-coding-agent`) with planning, file/diff/shell tooling, and
  sub-agent spawn. The operator asked for parity.
- **Plan (skeleton)**:
  - Define a `CodingAgent(PiAgent)` subclass with a curated tool
    bundle: `read_file`, `write_file`, `apply_patch`, `shell`,
    `search`, `list_dir`. Plus `plan(steps[])` and `subagent` tools.
  - Dedicated session schema variant that tracks the working
    directory + open files + planned/completed steps.
  - Dashboard tab "Code" with: file tree, current-file viewer,
    diff preview, plan progress bar.
  - Tests: end-to-end that the agent reads a file, proposes a patch,
    applies it under an approval gate, runs tests.
- This is genuinely a multi-week effort; track as its own milestone
  with sub-tasks.

### G. Session compaction ✗
- openclaw's biggest performance lever. Truncate old turns into a
  summary checkpoint, keep the tail. Reduces prompt size, latency,
  cost.
- pi-runtime already has a `truncating_summarizer` plugged into
  `LegacyContextEngine.compact`; need to expose this as an explicit
  RPC + UI control.

### H. Usage / cost RPC + UI ✗
- `usage.cost`, `usage.status` from openclaw — token + cost roll-up
  per session. Quietly aggregate from the existing `CacheObserver`
  data; surface in a new "Usage" tab.

### I. Plan-mode streaming events ✗
- openclaw's `emitAgentPlanEvent({ phase, title, steps })` lets the
  UI render the model's plan before execution. Useful for the
  coding-agent flow but applies to all agents.

### J. Multi-agent / sub-agent spawn (ACP-style) ✗
- openclaw spawns child agents via the Agent Control Plane. Less
  urgent for in-house dashboard use but unlocks complex flows.

---

## Parallelism map

|              | A | B-backend | B-frontend | C | F |
|--------------|---|-----------|------------|---|---|
| `app.js`     | ✱ |           | ✱          |   |   |
| `pi_agent.py`|   | ✱         |            | ✱ | ✱ |
| `history.py` |   | ✱         |            |   |   |
| `tools_pkg/` |   |           |            | ✱ | ✱ |
| `app.css`    |   |           | ✱          |   |   |

- **Now**: A and B-backend in parallel (no file overlap).
- **Then**: B-frontend (after A merges, otherwise app.js conflicts).
- **Then**: C (depends on a stable PiAgent surface from B-backend).
- **Later**: F as its own milestone.

---

## Done in earlier session (already shipped, see HANDOFF.md)
- Telegram extension removed.
- Dashboard channel `outbound_only=True`.
- PiAgent ConversationHistory bridge (chat.history populated).
- `ChatSendParams.{channel,account_id,chat_id}` `min_length=1`.
- Gateway dual-stack 127.0.0.1 + ::1.
- websockets handshake-failed log demoted to WARNING.
- `format_skills_for_prompt` `<usage>` hint ("skills are docs, not
  callable tools").
- Desktop client: custom-protocol fix, native HTTP/1.1 probe IPC,
  file-based token store, single-instance, dual-tray fix, build
  helper script.
