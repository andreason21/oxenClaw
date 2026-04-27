# Handoff — 2026-04-27 session

Single working session that converted oxenClaw from a Telegram-first
multi-channel project to a dashboard + native-desktop in-house assistant
with Slack-only outbound, debugged the Windows cross-build, then
spent a long second arc closing the openclaw parity gap on the
agent / tool / dashboard surface. This file is the next-session
starter pack.

---

## Top-line additions in this session (commit order)

| Commit | What |
|---|---|
| `0260f9b` | Telegram extension removed; project pivots to dashboard + Slack outbound |
| `c6eaa91` | Gateway / agent runtime hardening (validation, dual-stack, log-level) |
| `38b868d` | Desktop Windows client cross-build fixes (custom-protocol, native HTTP/1.1 probe, file token store, single-instance, dual-tray) |
| `0418e74` | P0-A "+ New chat" + P0-B tool-call telemetry cards |
| `61be156` | Document why /healthz only answers GET (won't fix) |
| `154be38` | P1-C `skill_resolver` (intent → search → install → instructions) |
| `b2b6a48` | P2-G `sessions.compact` RPC + Compact button |
| `6c5be70` | P2-H `usage.session` / `usage.totals` + cost roll-up |
| `47667ff` | P2-F skeleton: `CodingAgent` + `fs_tools` + `docs/CODING_AGENT.md` |
| `f52f1df` | K. Cron tab quick-add wizard with preset cards |
| `63a1e86` | F-1+F-6+F-7: `edit` tool + line-numbered `read_file` + `grep`/`glob` split + `read_pdf` |
| `8cb13bb` | F-2: `process` tool (start / send_keys / read_output / stop / list) |
| `0ba5a21` | F-3: structured `update_plan` tool + `plan.get` / `plan.list` RPCs |
| `6c4007a` | F-4 sessions tool family (status/list/history/send/spawn/yield) + F-5 subagents audit |
| `e0c3120` | L. `web_search` → `web_fetch` chaining (system prompt + recovery hint + multi-backend env-var pickup) |
| `d1c3932` | M. Drop Telegram-era 5-input chat target row → compact `Agent ▼ + chat-id chip + ⚙️ Advanced` bar |

Test status at session end: **1134 passed / 28 skipped / 0 failed** (Python suite, sandbox-infra and Playwright dashboard E2E auto-skip on this WSL2 box). Rust desktop tests **6 passed**.

---

## What changed (file-by-file, scope-grouped)

### 1. Telegram removal

User direction: project is dashboard + Win/Ubuntu desktop in/out, Slack
outbound-only.

**Deleted**

- `oxenclaw/extensions/telegram/` (entire bundled plugin — 12 files)
- `tests/test_telegram_*.py`, `tests/test_e2e_telegram_echo.py` (12 files)

**Edited**

- `pyproject.toml` — dropped `aiogram` dep, the `telegram` plugin entry
  point, the `"telegram"` keyword, and the `Telegram` mention in the
  `integration` pytest marker
- `oxenclaw/cli/message_cmd.py` — default channel `telegram` → `dashboard`
- `oxenclaw/cli/gateway_cmd.py` — comment cleanup
- `oxenclaw/gateway/protocol.py` — docstring + comment cleanup
- `oxenclaw/plugins/registry.py` — docstring cleanup
- `oxenclaw/plugin_sdk/reply_runtime.py` — docstring example switched to
  Slack's 40k char limit
- `oxenclaw/channels/runner.py` — docstring cleanup
- `oxenclaw/extensions/dashboard/__init__.py`, `extensions/slack/{accounts,token}.py`
  — docstring cleanup
- `oxenclaw/static/app.js` — cron form default channel + "no channels
  loaded" hint now reference Slack/dashboard
- `oxenclaw/tools_pkg/{cron_tool,message_tool}.py` — example channel id
  in `description=` now `'slack', 'dashboard'`
- 30+ test files: bulk-replaced literal `"telegram"` → `"dashboard"` and
  `telegram:main` → `dashboard:main` (channel-agnostic placeholder
  swaps; rewired `tests/test_gateway_channels_methods.py` and
  `tests/test_cli_gateway_wiring.py` to use a `_StubChannel` /
  `SlackChannel` instead of `TelegramChannel`)
- `tests/test_plugin_manifest.py` — asserts on `SLACK_PLUGIN` instead
  of the deleted `TELEGRAM_PLUGIN`

**Docs**

- `README.md` — replaced Telegram-setup section with "Optional — wire
  Slack outbound alerts", architecture diagram, channel-table row,
  Status-table row, KR mirror everywhere
- `docs/CONFIG_EXAMPLE.yaml` — full rewrite for dashboard + Slack-only
- `docs/INSTALL_WSL.md` — section 10 retitled "Slack outbound on WSL2"
  (EN + KR), prerequisite line updated
- Historical `docs/PORTING_PLAN.md`, `docs/ARCHITECTURE.md` left
  alone on purpose (planning artifacts; rewriting them rewrites
  history)

### 2. Validation hardening (gateway side)

Triggered by `:main no channel plugin` log warnings the user surfaced.

- `oxenclaw/gateway/protocol.py` — `ChatSendParams.channel`,
  `account_id`, `chat_id` get `Field(min_length=1)`. Empty values from
  the dashboard input are now rejected at the RPC boundary with a
  clean `string_too_short` error instead of falling through to the
  dispatcher and producing `:main` drop-warnings.
- `oxenclaw/static/app.js` — chat-target inputs (channel/account_id/
  chat_id/agent_id) snap back to the previous value if the user blanks
  them out, so a stray clear-and-tab doesn't persist `""` to
  `localStorage`.
- `oxenclaw/agents/dispatch.py:121` — the "could not deliver to X"
  drop-outbound log was downgraded `WARNING` → `INFO`. The structured
  `delivery_warnings` field already surfaces it to the RPC caller and
  the agent's reply IS in `chat.history`; logging at WARNING was noise
  for the documented dashboard path.

### 3. Dashboard channel correctness

- `oxenclaw/extensions/dashboard/channel.py` — added `outbound_only =
  True`. Without it the supervisor busy-restarts `monitor()` (the
  dashboard has no inbound stream — `chat.send` is the only entry),
  flooding logs with `monitor dashboard:main returned without stop;
  restarting in N s`.
- `oxenclaw/cli/gateway_cmd.py` — comment now says `(Slack, …)` since
  Telegram is gone.

### 4. PiAgent ↔ dashboard history bridge

User-reported "chat.send ok but chat.history empty" — agent ran but
dashboard never saw the reply.

- `oxenclaw/agents/pi_agent.py` — every turn now writes the user
  message and the assistant reply to `ConversationHistory`
  (`~/.oxenclaw/agents/<id>/sessions/<session_key>.json`) in addition
  to the pi-runtime `SessionManager`. The two stores are intentionally
  separate: pi keeps the rich transcript the runner needs, dashboard
  reads a flat role/content list.
- `tests/test_pi_agent.py` — new regression
  `test_pi_agent_writes_dashboard_conversation_history` confirms the
  session file shows up with both `user` and `assistant` entries.

### 5. Gateway IPv6 dual-stack + log de-noising

- `oxenclaw/gateway/server.py:349` — when bound to default
  `127.0.0.1`, also listens on `::1`. WebView2 / browsers that follow
  Happy Eyeballs and prefer `[::1]` no longer get
  ECONNREFUSED while curl/PowerShell (which fall back to v4) succeed.
- `oxenclaw/observability/logging.py` — `websockets.server` and
  `websockets.asyncio.server` loggers pinned at WARNING. Every TCP
  port-probe (port scanner, half-broken proxy, our own desktop-app
  reachability probe in earlier iterations) emits a full ERROR
  traceback for `did not receive a valid HTTP request` — noise now
  suppressed.

### 6. Skill prompt — "skills are docs, not tools"

Triggered by user reporting `tool 'stock-analysis' is not registered`
errors after a clawhub skill install.

- `oxenclaw/clawhub/loader.py:format_skills_for_prompt` — prepended a
  `<usage>` block telling the LLM that skills are reference material,
  not callable tools, and that the right path is to read SKILL.md and
  invoke documented scripts via the shell tool.
- `tests/test_clawhub_loader.py` — new regression
  `test_format_skills_block_includes_usage_hint` enforces the hint
  stays present.

### 7. Desktop client — Windows Tauri 2 fixes

The long debug arc. Listing in the order the bugs surfaced.

- **Connection-refused on launch (auto-redirect to dead URL)** —
  `desktop/web/index.html` now probes `/healthz` before navigating;
  on failure stays on the setup card and shows a "Start gateway in
  WSL and retry" button that calls `launch_wsl_gateway` IPC.
- **IPv4 default URL** — setup-card default `localhost` → `127.0.0.1`
  (paired with the gateway dual-stack bind to cover both paths).
- **Stored-URL sanitiser** — `desktop/src-tauri/src/main.rs` got a
  `force_ipv4_loopback` helper applied in `connect_url` /
  `connection_info`. Saved `localhost` URLs from earlier sessions get
  rewritten to `127.0.0.1` on read.
- **Tray icon — two icons → one** — `desktop/src-tauri/tauri.conf.json`
  had a declarative `trayIcon` block that Tauri 2 turned into one
  tray; `main.rs:build_tray()` programmatically built another. Removed
  the JSON block; `build_tray()` now also calls
  `.icon(app.default_window_icon().cloned()).tooltip("oxenClaw")`
  explicitly.
- **Single-instance** — added `tauri-plugin-single-instance` (Cargo +
  `main.rs` registration). Repeated double-clicks bring the existing
  window to front instead of accumulating zombies.
- **Tauri runtime not on `window.__TAURI__`** — race against
  `withGlobalTauri` injection. `index.html` now falls back to
  `window.__TAURI_INTERNALS__.invoke` (always present) and prints
  which API surface won at boot.
- **Auto-connect disabled on diagnostic builds** — the auto-redirect
  hid the diag/alert before the user could read it. Setup form
  always shown; `tryAutoConnect` now only prefills the URL.
- **`devUrl` fallback (THE root cause of every "still doesn't work")** —
  `cargo xwin build --release` was producing a binary that runtime-
  loaded `http://localhost:1420` (the `devUrl`) because the Tauri
  `custom-protocol` feature wasn't enabled. The user never saw our
  index.html. Fix: `scripts/build-windows-exe.sh` now passes
  `--no-default-features --features custom-protocol`. `cargo tauri
  build` injects this automatically; raw `cargo xwin build` does not.
  `desktop/README.md` carries an explicit "Critical" callout.
- **Probe via Rust IPC instead of `fetch()`** — `tauri://localhost` →
  `http://127.0.0.1:7331` is cross-origin and Chromium's CORS / PNA
  blocks the response from reaching JS even when PowerShell sees 200.
  New `probe_gateway` IPC opens a real TCP connection, sends `GET
  /healthz HTTP/1.1` (HTTP/1.0 the websockets-library silently
  closes; HEAD ditto), parses the status line. Returns a granular
  status string (`ok ...`, `connect_failed ...`, `read_empty ...`,
  `non_2xx ...`, `dns_resolve_failed ...`) the alert popup surfaces
  verbatim.
- **Token storage — keyring → file** — `keyring` v3 silently no-ops
  in cargo-xwin cross-builds (`set_password` returns Ok, never
  persists). Replaced with a JSON file at
  `%LOCALAPPDATA%\oxenclaw-desktop\token.json` (Linux:
  `$XDG_DATA_HOME/oxenclaw-desktop/token.json`, mac:
  `$HOME/Library/Application Support/oxenclaw-desktop/token.json`).
  NTFS ACLs already restrict read to the same user. Production builds
  via `cargo tauri build` should reintroduce keyring once the
  cross-build lossiness is understood; tracked as a TODO. New tests:
  `save_then_load_round_trip`, `load_returns_none_when_file_missing`.
- **Helper script** — `scripts/build-windows-exe.sh` cross-builds the
  raw .exe via `cargo xwin build` AND copies it to
  `%LOCALAPPDATA%\oxenclaw-dev\oxenclaw-desktop.exe`. Output prints
  both paths and warns "do NOT run from `\\wsl$\...`" (the UNC
  hypothesis turned out to be wrong but the local copy is still
  better practice).

---

## Test status (end of session)

- **Python**: 1134 passed, 28 skipped, 0 failed.
  Skips: `tests/test_shell_tool.py` (bwrap unavailable on this WSL2
  box — environmental), 26 dashboard E2E (Playwright auto-skip; runs
  in CI), 1 misc env-gated.
- **Rust desktop**: 6 passed (`force_ipv4_loopback` 2, `probe_gateway`
  2, token round-trip 2).
- **Live smoke** against a fresh gateway (`provider=echo`, port 17331):
  `chat.send` through `dashboard:main` returns `status="ok"` with a
  real `dashboard-...` message_id; `channels.list` shows only
  `dashboard`; empty-channel rejected with `string_too_short`;
  `telegram` channel falls back to `message_id="local"` with delivery
  warning. PiAgent end-to-end via Ollama gemma4 leaves both `user`
  and `assistant` rows in `chat.history`.

---

## Open items / known caveats

1. **Desktop signing + production token storage** — restore `keyring`
   for the signed `cargo tauri build` path. The cross-build's silent
   `set_password` no-op was working around it via file storage; the
   release pipeline should not ship a build that drops DPAPI without
   confirming the tradeoff.
2. **Gateway HEAD support — won't fix.** `websockets/http11.py:150`
   hard-rejects non-GET. Documented inline + in ROADMAP P1-E.
3. **`tests/test_shell_tool.py` skip** — bwrap on this WSL2 host fails
   with "Creating new namespace failed". Investigate before relying
   on the sandbox path here.
4. **CodingAgent ~65–70% openclaw parity.** What's still missing:
   dashboard "Code" tab UI (file tree + diff viewer + plan progress
   bar), `apply_patch` (unified-diff), plan-event WS stream, real
   sub-agent spawn matching openclaw's ACP semantics (`subagent.py`
   today is a synchronous task delegator — see
   `docs/SUBAGENTS_AUDIT.md` for the 5-axis comparison).
5. **`<plan>` parsing path decision pending.** CodingAgent's prompt
   tells the model to use `update_plan(...)` (structured tool) but
   doesn't strip / repurpose the freeform `<plan>...</plan>` text
   block legacy clients may still emit. See `docs/CODING_AGENT.md`
   "Biggest open question".
6. **Skill prompt hint is best-effort.** Strong models obey the
   "skills are docs not callable tools" guidance + the
   `skill_resolver` fallback; weaker models may still hallucinate.
   If a recurring "tool 'X' is not registered" pattern shows up,
   consider auto-registering shell-tool wrappers from skill
   `commands:` frontmatter, or filtering user skills that ship a
   `commands:` block at load time.

---

## Quick-start for the next session

```bash
# Run the suite
oxenclaw/.venv/bin/pytest tests/ -q \
    --ignore=tests/integration --ignore=tests/dashboard --ignore=tests/test_shell_tool.py

# Rebuild the Windows .exe (cross-compile from WSL2)
bash scripts/build-windows-exe.sh
# Output binary: C:\Users\<you>\AppData\Local\oxenclaw-dev\oxenclaw-desktop.exe
# Critical: must include `--features custom-protocol` (the script does)

# Run the gateway (production-style)
OXENCLAW_GATEWAY_TOKEN=$(openssl rand -hex 32) \
    oxenclaw/.venv/bin/oxenclaw gateway start --provider ollama
# Dashboard: http://127.0.0.1:7331/?token=<TOKEN>
# Token from: ~/.oxenclaw/gateway-token (rc.16+)

# Wipe desktop-app state for a clean repro
powershell.exe -NoProfile -Command 'Get-Process -Name oxenclaw* -ErrorAction SilentlyContinue | Stop-Process -Force'
powershell.exe -NoProfile -Command 'Remove-Item -Recurse -Force "$env:LOCALAPPDATA\ai.oxenclaw.desktop","$env:LOCALAPPDATA\oxenclaw-desktop" -ErrorAction SilentlyContinue'
```

Repository invariant the user emphasised: **code change → test → docs is
one set**. Memory: `~/.claude/projects/.../memory/feedback_change_set.md`.
