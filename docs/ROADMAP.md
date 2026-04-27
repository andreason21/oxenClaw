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

### C. Skill discovery → install → invoke pipeline ✓
**Shipped**.

  - **Tool**: new `skill_resolver(query, auto_install=True)` callable
    tool in `oxenclaw/tools_pkg/skill_resolver_tool.py`. Returns one
    of: `found="installed"` (with skill_md path + scripts_dir +
    instructions excerpt), `found="none"`, `found="error"`, or
    `found="remote_only"` (search hit but no installer available).
    Match logic: installed skills first by name/slug/description
    word match; then `MultiRegistryClient.get_client().search_skills`
    against ClawHub; auto-install via `SkillInstaller.install` (handles
    "already installed" gracefully).
  - **Wiring**: `oxenclaw/cli/gateway_cmd.py` registers the tool with
    the gateway's tool_registry once the `MultiRegistryClient` and
    `SkillInstaller` are built; available to every PiAgent turn.
  - **Prompt**: `format_skills_for_prompt` `<usage>` block now
    explicitly says "if the user's request implies a domain not
    covered by the skills listed below, call `skill_resolver(...)`",
    distinguishing it from the (still-correct) "skills are docs not
    callable tools" guidance.
  - **Tests**: 3 in `tests/test_skill_resolver_tool.py` covering
    installed-match, no-match-no-registries, and install-via-fake-
    registry paths.

### C-historical plan (kept for context)
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

### E. HEAD support on `/healthz` — *won't fix* (library constraint)
The underlying `websockets` library rejects any non-GET method at the
HTTP-parsing stage (`websockets/http11.py:150`: `method != b"GET"` →
ValueError), so HEAD probes get the socket closed before our
`process_request` callback sees them. Forking the library to
support HEAD isn't worth it; modern liveness probes default to GET.
Documented inline in `gateway/server.py:serve_healthz` and the
session HANDOFF so the next operator doesn't chase this.

---

## P2 — large efforts

### F. Pi-coding-agent equivalent ◐ (skeleton shipped)
**Skeleton shipped — full plan in `docs/CODING_AGENT.md`.**

  - **Class**: `CodingAgent(PiAgent)` in
    `oxenclaw/agents/coding_agent.py` — plan-first system prompt,
    curated tool bundle, approval-gated writes when an
    `ApprovalManager` is injected.
  - **Tools**: new `oxenclaw/tools_pkg/fs_tools.py` ships
    `read_file`, `write_file`, `list_dir`, `search_files`,
    `shell_run`. Read-only tools never gate; writes/shell gate
    when an approval manager is present.
  - **Factory**: `build_agent(agent_type="coding")` routes to
    `CodingAgent`. CLI flag still TODO.
  - **Tests**: 3 in `tests/test_coding_agent.py`.

**Deferred to the next session** (see CODING_AGENT.md "Deferred"):
dashboard "Code" tab, `apply_patch` tool, plan-event WS stream,
`--agent-type` CLI flag, sub-agent spawn.

### G. Session compaction ✓
**Shipped**.

  - **RPC**: `sessions.compact` in `oxenclaw/gateway/sessions_methods.py`;
    params `id`, `keep_tail_turns=6`, `reason` (optional); calls
    `decide_compaction` + `apply_compaction` with `truncating_summarizer`
    and persists the new `CompactionEntry` onto the session via `sm.save`.
  - **UI**: "Compact" button added per-row in `SessionsView` (`app.js`);
    confirms before calling, toasts `tokens_before → tokens_after` on
    success or "below threshold — nothing to do" when no compaction ran.
  - **Tests**: `test_compact_below_threshold_is_noop` and
    `test_compact_summarises_old_turns` in
    `tests/test_gateway_sessions_methods.py` using `InMemorySessionManager`.

### H. Usage / cost RPC ✓ (UI tab pending)
**Backend shipped**.

  - **RPCs**: `usage.session(agent_id, session_key)` and
    `usage.totals(agent_id?)` in
    `oxenclaw/gateway/usage_methods.py`. Reads sibling files at
    `~/.oxenclaw/agents/<agent>/sessions/<key>.usage.json`.
  - **PiAgent.persist_usage**: writes the cumulative
    `(turns, input, output, cache_read, cache_create, hit_rate,
    cost_usd)` snapshot after each turn. Cost is computed from the
    model's `pricing` dict (USD per million tokens, mapped from
    pricing keys like `input_tokens` to summary fields).
  - **Tests**: 4 in `tests/test_gateway_usage_methods.py` cover
    missing-file zeros, single-session read, cross-agent
    aggregation, and `agent_id` filter.

**Pending**: dashboard "Usage" tab (table per agent + total card +
hit-rate chart). RPC payloads are stable, so the UI work can land
as a follow-up without backend churn.

### I. Plan-mode streaming events ✗
- openclaw's `emitAgentPlanEvent({ phase, title, steps })` lets the
  UI render the model's plan before execution. Useful for the
  coding-agent flow but applies to all agents.

### J. Multi-agent / sub-agent spawn (ACP-style) ✗
- openclaw spawns child agents via the Agent Control Plane. Less
  urgent for in-house dashboard use but unlocks complex flows.

### N. Cron tab full openclaw parity ✓
**Shipped** — operator asked to bring the Cron tab UX to openclaw
parity. Both backend and frontend in this batch.

  Backend
  - `oxenclaw/cron/run_log.py` (new): `CronRunStore` JSON-backed
    store (`<paths.home>/cron/runs.json`) with append / update /
    list / total / prune (default 100 runs/job). Atomic write,
    cursor-based filtering by job_id / status / delivery / query
    substring / sort_dir.
  - `CronScheduler._fire` now writes a "running" `CronRunEntry`
    before dispatch and updates it with status / delivery_status /
    error / ended_at on completion or exception.
  - Three new RPCs in `oxenclaw/gateway/cron_methods.py`:
      `cron.runs` → `{ runs, total, has_more }` with full filter set
      `cron.run_status({ run_id })` → single entry or null
      `cron.update({ id, schedule?, prompt?, agent_id?, channel?,
                    account_id?, chat_id?, thread_id?, name?,
                    description?, enabled? })` → `{ ok, job }`
  - Tests: 13 new in `tests/test_cron_run_log.py` (store contract)
    + 8 in `tests/test_gateway_cron_runs.py` (RPC end-to-end).

  Frontend (oxenclaw/static/app.js + app.css)
  - 820-line CronView rewrite. Job list filter row: search,
    enabled/disabled, schedule kind, last-status, sort-by, sort
    direction toggle, reset.
  - Status pills per row: enabled/disabled + last-run pill (✓/✗/⏭/·)
    + next-run countdown. Per-row actions: Edit / Clone / Toggle /
    Run / History / Remove.
  - Full edit modal (Basics / Schedule / Execution / Advanced
    sections) with `aria-invalid` + clickable blocking-field list.
  - Run-log sub-panel (new "Run log" tab next to "Jobs"): scope +
    status multi-checkbox + delivery filter + search + Load more
    pagination, expandable output / error blocks per run.
  - Quick-add wizard (K) preserved as the casual-user entry path;
    a new "Advanced new job" button opens the full modal.
  - Tests: 3 new in `tests/dashboard/test_dashboard_e2e.py`
    (search filter, modal validation, run-log pills).

Suite: 1157 passed / 31 skipped / 0 failed.

### M. Compact chat-target bar (drop Telegram-era 5-input row) ✓
**Shipped** — operator pointed out that agent_id, channel,
account_id, chat_id, thread_id displayed in the chat tab were
mostly noise: only `agent_id` and `chat_id` carry user-visible
meaning; the other three default to `dashboard:main:""`
unconditionally and were Telegram-era leftovers.

  - Default chat tab now renders a compact target bar: Agent
    `<select>` (populated from agents.list at render) + chat-id
    chip (`💬 chat-...`, click to rename / new / pick) + ⚙️
    Advanced toggle.
  - Click the chip → `prompt()` to set a new chat-id; blank input
    triggers ChatState.newChat() so the chip doubles as the
    "+ New chat" affordance for keyboard users.
  - The full five-input row still lives behind the Advanced
    toggle for debugging multi-channel routing or non-default
    accounts. `samp:new-chat` listener and the sessions-panel
    click handler keep the chip and advanced inputs synchronised.
  - Dashboard E2E: `test_chat_view_compact_target_bar_default_only_shows_agent_and_chip`.

### L. web_search → web_fetch chaining + multi-backend fallback ✓
**Shipped** — operator reported that openclaw answers
"AI 반도체 시장 전망 2026" by chaining web_search → web_fetch
through several URLs (even surfacing 404s) while oxenClaw stopped
at "no results (tried providers: duckduckgo)".

  - `PiAgent.DEFAULT_SYSTEM_PROMPT` now contains an explicit
    "Web research playbook" mirroring openclaw's chaining guide:
    web_search first, then web_fetch on a likely URL when search
    is empty/insufficient, retry with rephrased queries, treat
    404 as data not a stop signal.
  - `web_search_tool` no-results path returns recovery suggestions
    (call web_fetch, try alternate phrasings, list of env vars
    that would add backends) instead of a bare "no results".
  - New `build_default_search_chain()` in `tools_pkg/web.py` reads
    `BRAVE_API_KEY` / `TAVILY_API_KEY` / `EXA_API_KEY` /
    `SEARXNG_URL` from env and prepends those providers ahead of
    DuckDuckGo, so dropping a key into the gateway env immediately
    upgrades search quality.
  - Tests: `test_web_search_zero_hits_returns_recovery_hint` +
    `test_build_default_search_chain_picks_up_env_keys`.

### K. Cron tab quick-add wizard ✓
**Shipped**.

  - Topbar `+ New job` button + prominent quick-add card at the top
    of the Cron tab. Preset grid: Every morning / Every evening /
    Hourly / Weekdays / Weekly / Every 5 minutes — each card maps
    to a 5-field cron expression internally so users don't have to
    write `0 8 * * *` themselves.
  - Quick form has just three inputs (prompt textarea, agent_id,
    chat_id); defaults to the active ChatState values. The advanced
    5-field form below stays for power users / non-preset
    schedules.
  - CSS `.cron-preset` / `.cron-preset-grid` mirrors openclaw's
    cron-quick-create.ts visual idiom.
  - Dashboard E2E:
    `test_cron_view_quick_add_wizard_renders_presets` (presets
    count, active-state toggle, topbar button).

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
