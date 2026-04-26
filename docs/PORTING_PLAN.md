# sampyClaw Porting Plan

A phased roadmap for porting [openclaw](https://github.com/openclaw/openclaw) (TypeScript monorepo, ~2.46M LOC) to Python. Companion to [`ARCHITECTURE.md`](./ARCHITECTURE.md).

## Scope

**In scope:**
- `src/gateway/` (JSON-RPC/WebSocket control plane)
- `src/plugin-sdk/` (public plugin contract)
- `src/plugins/` (plugin loader, registry)
- `src/channels/` (channel abstraction)
- `src/agents/` (agent harness, tool invocation)
- `src/cli/` (`openclaw` command)
- `src/config/` (YAML config, validation)
- `extensions/telegram/` (B-phase pilot)
- Additional channel plugins post-B

**Out of scope (permanent):**
- `ui/` — stays Vue/TS or gets a separate Python web UI later.
- `apps/ios/`, `apps/android/`, `apps/macos/`, `Swabble/` — native apps cannot be Python.
- Node-specific build tooling (`tsgo`, `tsdown`, `vitest`, `pnpm`) — replaced by Python equivalents (`mypy`/`pyright`, `hatch`/`uv`, `pytest`).

## Phases

### Phase D — Architecture analysis  *(done)*

Output: [`ARCHITECTURE.md`](./ARCHITECTURE.md). Reference spec for all subsequent work.

### Phase B — Telegram proof-of-concept

Goal: prove the port pattern end-to-end with one real channel. When a real Telegram bot can receive and reply through sampyClaw, the pattern is validated.

**B.1 — Project scaffold.**
- `pyproject.toml` (hatch build backend, Python 3.11+).
- `sampyclaw/` top-level package.
- Dev tooling: `pytest`, `pytest-asyncio`, `ruff`, `pyright`, `pre-commit`.

**B.2 — Plugin SDK foundation** (`sampyclaw/plugin_sdk/`).
- `channel_contract.py` — `ChannelPlugin` Protocol/ABC mirroring `src/channels/plugins/types.plugin.ts`.
- `config_schema.py` — base Pydantic models for plugin config.
- `config_runtime.py` — config loader (`load_config()`, `resolve_channel_group_policy()`).
- `runtime_env.py` — logger, env helpers.
- `reply_runtime.py` — reply dispatch + chunking primitives.
- `media_runtime.py` — media envelope types.
- `error_runtime.py` — error taxonomy.

**B.3 — Config & credential store** (`sampyclaw/config/`).
- YAML load/validate (`config.yaml`).
- Credential read/write (`~/.sampyclaw/credentials/<channel>/<accountId>.json`).
- Env var substitution (`$TELEGRAM_BOT_TOKEN`).
- Migration stubs (not needed for greenfield Python but keep shape).

**B.4 — Gateway protocol** (`sampyclaw/gateway/`).
- `protocol/schemas.py` — Pydantic models for RPC params/results (minimum needed for Telegram flow).
- `server.py` — FastAPI + `websockets` JSON-RPC server.
- `router.py` — method dispatch.

**B.5 — Telegram extension** (`sampyclaw/extensions/telegram/`). File-for-file mirror of `openclaw/extensions/telegram/src/`:
- `manifest.json` ← `openclaw.plugin.json`.
- `channel.py` ← `channel.ts` — plugin definition + routing.
- `bot_core.py` ← `bot-core.ts` — `aiogram`-backed bot factory, update dedup.
- `bot_message_context.py` ← `bot-message-context.ts`.
- `bot_handlers.py` ← `bot-handlers.runtime.ts` — message/callback/reaction dispatch.
- `bot_message_dispatch.py` ← `bot-message-dispatch.ts`.
- `monitor.py` + `monitor_polling.py` + `monitor_webhook.py`.
- `send.py` ← `send.ts` — outbound send, 429 backoff, media.
- `accounts.py`, `token.py`.
- `format.py` ← `format.ts` — MarkdownV2/HTML.
- `targets.py`, `normalize.py`, `thread_bindings.py`.
- `polling_session.py`, `polling_transport_state.py`.
- `network_errors.py`, `request_timeouts.py`.
- `action_runtime.py` ← `action-runtime.ts`.

**B.6 — Minimal agent** (`sampyclaw/agents/`).
- Echo agent for integration testing.
- Tool schema → Pydantic.
- Inference loop stub (actual LLM calls via `anthropic` SDK).

**B.7 — CLI** (`sampyclaw/cli/`).
- `typer` app with `gateway start`, `message send`, `config get/set`.

**B.8 — Test harness.**
- Mock Telegram API via `respx` (`aiogram` uses `aiohttp`).
- Port `extensions/telegram/src/test-support/` fixtures.
- Integration test: fake Telegram → gateway → echo agent → outbound call verified.

**Exit criteria for B:** `pytest` green, a real Telegram bot runs via `sampyclaw gateway start` and echoes messages end-to-end.

### Phase A — Core expansion

Only start after B exit criteria met.

**A.1** — Real agent harness (tool registry, provider SDKs: Anthropic + OpenAI).
**A.2** — Second channel (Discord or Slack) to validate SDK generality.
**A.3** — Canvas host runtime (if needed).
**A.4** — Gateway protocol completion (all RPC methods, event framing).
**A.5** — Cron scheduling, approval prompts.
**A.6** — Plugin discovery from third-party packages (entry points via `importlib.metadata`).

### CV-1 — Dashboard canvas

Status: **Shipped 2026-04-26**. ~600 LOC across `sampyclaw/canvas/{errors,store,events}.py` + `sampyclaw/gateway/canvas_methods.py` + `sampyclaw/tools_pkg/canvas.py` + ~150 LOC of dashboard SPA additions + `sampyclaw/skills/canvas/SKILL.md`. 39 new unit tests + 1 live `gemma4:latest` integration test. Architecture: see [`CANVAS.md`](./CANVAS.md).

What this replaces from openclaw `src/canvas-host/` (~16K LOC of Tailscale-aware HTTP host + bridge + a2ui bundle): the LLM-callable canvas surface, collapsed onto the existing dashboard. Native-node / Tailscale / live-reload / a2ui are out of scope by design. **Empirical gate: gemma4:latest scored 25/25 on canvas tool calls before commit.**

### BR-1 — Browser tools (Playwright, fail-closed)

Status: **Shipped 2026-04-26**. ~900 LOC across `sampyclaw/browser/{policy,errors,pinning,egress,session}.py` + `sampyclaw/tools_pkg/browser.py` + `sampyclaw/skills/browser/SKILL.md`. 29 new tests. Architecture: see [`BROWSER.md`](./BROWSER.md).

What this replaces from openclaw `extensions/browser/` (~24K LOC, 156 files): the LLM-callable subset of `pw-tools-core.*` (`navigate`, `snapshot`, `screenshot`, `click`, `fill`, `evaluate`, `download`) and the egress security from `navigation-guard.ts` + `request-policy.ts` + `cdp-reachability-policy.ts`. The CDP bridge / `chrome-mcp` / `qa-lab` surface is intentionally out of scope — sampyClaw exposes browser tools in-process, not as a remote control plane.

## Risks & open decisions

| Decision | Options | Default |
|---|---|---|
| Config/credential dir | `~/.openclaw/` (compat) vs `~/.sampyclaw/` (clean) | **`~/.sampyclaw/`** — clean break; write a one-shot importer later if needed |
| Telegram library | `aiogram` vs `python-telegram-bot` | **`aiogram`** — native asyncio, closer to `grammy`'s ergonomics |
| Gateway framework | `FastAPI` vs `aiohttp` | **`FastAPI`** — Pydantic-native, better tooling |
| CLI framework | `typer` vs `click` | **`typer`** — type hints, less boilerplate |
| Build backend | `hatch` vs `uv` vs `poetry` | **`hatch`** with `uv` for install speed |
| Case convention on wire | snake_case vs camelCase | **snake_case Python-side**, Pydantic `alias_generator=to_camel` for wire compat with TS |
| Schema validation | `pydantic` v2 vs `msgspec` | **`pydantic` v2** — maturity, error messages |

These defaults can be revisited — don't treat as locked.

## Effort estimate

Rough order of magnitude, assuming continuous focused work:

- Phase D: done.
- Phase B: **20–40 engineering hours** spread across multiple sessions. `send.ts` + `bot-handlers.runtime.ts` alone are ~3500 LOC of nuanced logic.
- Phase A: **100+ hours**. Highly variable depending on how many channels and how much gateway coverage is needed.

This is genuine engineering work, not a mechanical transliteration. Expect to revise the port when TS patterns don't translate cleanly.
