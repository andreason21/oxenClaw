# OpenClaw Architecture Analysis — oxenClaw Port Reference

> This document is the **frozen spec** for porting openclaw (TypeScript monorepo) to oxenClaw (Python). It was produced from a deep read of the upstream `openclaw/` source tree on 2026-04-24. Cross-reference the TS source as authoritative when this doc and the code disagree; then update this doc.

## 1. Top-Level Module Map

### `src/` (core runtime — **port target**)

Heart of openclaw. Gateway, plugin loader, agent harness, CLI.

- `src/gateway/` — JSON-RPC over WebSocket control plane. `src/gateway/protocol/` holds Zod schemas for the wire format (code-first, no `.proto`).
- `src/channels/` — Core channel abstraction. Plugins do NOT import this directly; they go through `src/plugin-sdk/`.
- `src/plugins/` — Plugin discovery, manifest parsing, registry. Manifest-first: metadata loaded before plugin code.
- `src/plugin-sdk/` — **Public contract** re-exported as `openclaw/plugin-sdk/*`. Channel plugins must only import from here.
- `src/agents/` — Agent harness, tool invocation, LLM inference loop.
- `src/cli/` — CLI (`openclaw` command). Entry: `openclaw.mjs`.
- `src/canvas-host/` — Live UI canvas host runtime.
- `src/config/` — YAML config parsing, Zod validation, migrations, schema generation.
- `src/commands/` — CLI subcommand implementations.

### `packages/` (publishable packages)

- `packages/plugin-sdk` — Re-export shim; real SDK in `src/plugin-sdk/`.
- `packages/plugin-package-contract` — `openclaw.plugin.json` manifest schema.
- `packages/memory-host-sdk` — Memory/vector store integration.

### `extensions/` (bundled plugins — **B-phase target: `telegram`**)

~115 npm packages. Channels (Telegram, WhatsApp, Discord, Slack, Signal, Matrix, iMessage, IRC, Google Chat, Twitch, …) and providers (Anthropic, OpenAI, Bedrock, Gemini, …).

### `ui/` (web UI — **out of scope**, stays JS)

Vue/TS dashboard.

### `apps/`, `Swabble/` (native apps — **out of scope**)

- `apps/ios/` Swift/SwiftUI
- `apps/android/` Kotlin
- `apps/macos/`, `Swabble/` Swift

### `scripts/`, `docs/`, `test/`

Build scripts, product docs, shared test helpers.

---

## 2. Core Runtime Flow — End-to-End Message

Example: Telegram user sends a message.

1. **Inbound reception** — `extensions/telegram/src/monitor.ts` polls Telegram via `grammy` Bot library (or webhook). `createTelegramBotCore()` registers update handlers.
2. **Context extraction** — `extensions/telegram/src/bot-message-context.ts` parses the Update, extracts text/media/sender/chat/thread, resolves DM vs group vs topic. `resolveTelegramSessionConversation()` binds the update to a conversation key.
3. **Envelope** — Channel adapter builds an `InboundEnvelope` per `src/channels/plugins/types.plugin.ts`. DM policy (`pairing` vs `open`) enforced here.
4. **Gateway dispatch** — Message flows through `src/gateway/*`. Operator CLI (`openclaw message send`) talks JSON-RPC to the gateway; gateway routes by `agents.<agentId>.channels.<channel>.allowFrom`.
5. **Agent execution** — `src/agents/*` spawns the agent with the inbound, calls the LLM provider, executes tool calls (including channel-native tools like Telegram reactions).
6. **Outbound** — `extensions/telegram/src/send.ts` `sendMessageTelegram()` formats and posts to `api.telegram.org/bot<token>/sendMessage` via fetch. Token resolved from `~/.openclaw/credentials/telegram/<accountId>.json`.
7. **Delivery** — Sent message ID cached (for edits/threading), session history updated.

Key files in this path:
- `extensions/telegram/src/bot-core.ts` — Bot factory, update dedup.
- `extensions/telegram/src/channel.ts` — Plugin definition, routing adapters.
- `extensions/telegram/src/bot-handlers.runtime.ts` — Update handlers (lazy-loaded).
- `extensions/telegram/src/send.ts` — Outbound send.
- `src/gateway/protocol/index.ts` — RPC schemas.
- `src/commands/agent.ts` — Agent CLI.

---

## 3. Plugin SDK Surface

`src/plugin-sdk/*`, published as `openclaw/plugin-sdk/<name>`.

**Public subpaths:**
- `channel-contract`, `channel-core`, `channel-lifecycle` — Channel plugin types.
- `setup`, `setup-runtime`, `setup-adapter-runtime` — Auth/config setup.
- `config-runtime`, `config-schema` — Config resolution.
- `runtime`, `runtime-env`, `runtime-logger` — Env, logging.
- `approval-runtime`, `approval-native-runtime` — Inline approval buttons.
- `media-runtime`, `media-mime`, `outbound-media` — File handling.
- `reply-runtime`, `reply-dispatch-runtime` — Chunking/delivery.
- `conversation-runtime`, `conversation-binding-runtime` — Thread binding.
- `error-runtime`, `ssrf-runtime` — Errors, SSRF policy.

**Channel plugin contract** (shape from `src/channels/plugins/types.plugin.ts`):
```ts
interface ChannelPlugin {
  id: string;
  send(p: SendParams): Promise<SendResult>;
  monitor(o: MonitorOpts): Promise<void>;
  probe(o: ProbeOpts): Promise<ProbeResult>;
  setup?: SetupAdapter;
  directory?: DirectoryAdapter;
  messageActions?: ChannelMessageActionAdapter[];
}
```

**Manifest** (`extensions/telegram/openclaw.plugin.json`):
```json
{
  "id": "telegram",
  "channels": ["telegram"],
  "channelEnvVars": { "telegram": ["TELEGRAM_BOT_TOKEN"] },
  "configSchema": { "type": "object", "additionalProperties": false, "properties": {} }
}
```

---

## 4. Gateway Protocol

- **Wire format:** JSON-RPC 2.0 over WebSocket. JSON only, no protobuf.
- **Schemas:** Zod validators, code-first, in `src/gateway/protocol/index.ts`. No `.proto` files.
- **Key RPC methods:** `chat.send`, `chat.history`, `agent.wait`, `agents.{list,create,delete}`, `channels.{start,logout}`, `config.{get,set,patch}`, `cron.*`, `exec-approvals.*`.
- **Event framing:** `EventFrame { type: "event"; body: ChatEvent | AgentEvent | ... }`.
- **Version:** `PROTOCOL_VERSION` constant (advisory, not strict).

---

## 5. Config & State

Location: `~/.openclaw/` (or `%APPDATA%\openclaw` on Windows).

```
~/.openclaw/
  config.yaml              # main config (YAML, Zod-validated)
  credentials/
    telegram/<accountId>.json  # tokens
    whatsapp/<phone>.json
  agents/<agentId>/
    agent/auth-profiles.json
    sessions/<sessionKey>.json
  plugins/                 # third-party plugin data
```

**Config shape highlights:**
- `channels.<id>.accounts` — account registration.
- `channels.<id>.allowFrom` — DM allowlist.
- `agents.<id>.channels.<channel>` — per-agent routing.
- `providers.<id>` — LLM provider config.

Migration: `src/config/legacy.ts` handles v1 → v2.

---

## 6. External Dependency Mapping (Node → Python)

| Node library | Purpose | Python equivalent |
|---|---|---|
| `express` + `ws` | HTTP + WebSocket server | `FastAPI` + `websockets` (or `aiohttp`) |
| `zod` | Schema validation | `pydantic` v2 |
| `ajv` | JSON Schema | `jsonschema` |
| `commander` | CLI parsing | `typer` (preferred) or `click` |
| `dotenv` | `.env` loading | `python-dotenv` |
| `croner` | Cron scheduling | `apscheduler` or `croniter` |
| `grammy` | Telegram Bot API | `aiogram` (preferred, native asyncio) or `python-telegram-bot` |
| `openai` | OpenAI SDK | `openai>=1.0` |
| `@anthropic-ai/sdk` | Anthropic SDK | `anthropic` |
| `sharp` | Image processing | `Pillow` |
| `sqlite-vec` | Vector search | `sqlite-vec` (has Python binding) |
| `linkedom` / `readability` | DOM parsing | `beautifulsoup4` + `readability-lxml` |
| `js-yaml` | YAML | `PyYAML` or `ruamel.yaml` |
| Node event loop | Async runtime | `asyncio` |

---

## 7. Telegram Extension Deep-Dive (B-Phase Target)

`extensions/telegram/` — ~394 source files.

### Directory layout

```
extensions/telegram/
├── openclaw.plugin.json          # manifest
├── package.json
├── index.ts                      # defineBundledChannelEntry()
├── channel-plugin-api.ts         # exports telegramPlugin
├── secret-contract-api.ts        # secret schema
├── runtime-api.ts                # heavy runtime, lazy-loaded
├── setup-entry.ts                # setup wizard
└── src/
    ├── channel.ts                # ~1400 LOC — plugin definition
    ├── bot-core.ts               # bot factory
    ├── bot.ts / bot.types.ts
    ├── bot-deps.ts               # DI (config loader, runtime)
    ├── bot-handlers.runtime.ts   # ~2000 LOC — update handlers
    ├── bot-message-context.ts    # context from Update
    ├── bot-message-dispatch.ts   # ~1500 LOC — route to agent
    ├── bot-native-commands.ts    # ~1400 LOC — /command menu
    ├── monitor.ts                # main event loop
    ├── monitor-polling.runtime.ts   # grammy Runner polling
    ├── monitor-webhook.runtime.ts   # webhook handler
    ├── send.ts                   # ~1500 LOC — outbound send
    ├── accounts.ts               # multi-account
    ├── token.ts                  # token resolve/rotate
    ├── channel-actions.ts        # reactions, etc.
    ├── approval-native.ts        # inline button approvals
    ├── exec-approvals.ts         # execution approval routing
    ├── draft-stream.ts           # streaming chunks
    ├── lane-delivery.ts          # delivery state machine
    ├── format.ts                 # Markdown/HTML formatting
    ├── targets.ts                # parse DM/group/topic
    ├── normalize.ts              # normalize IDs
    ├── thread-bindings.ts        # topic → conversation
    ├── polling-session.ts        # long-poll session
    ├── polling-transport-state.ts  # offset, backoff
    ├── network-errors.ts         # error classification
    ├── request-timeouts.ts
    ├── action-runtime.ts         # ~700 LOC — tool exec
    └── test-support/
```

### Entry point & registration

`extensions/telegram/index.ts` exports `defineBundledChannelEntry({ id: "telegram", plugin: {...}, runtime: {...}, secrets: {...}, accountInspect: {...} })`. Core registers `telegramPlugin` in the channel registry.

### Telegram API client

- **Library:** `grammy` (wraps Telegram Bot API; uses `fetch`).
- **Methods:** `bot.api.sendMessage()`, `editMessageText()`, `setWebhook()`, etc.
- **Transport:** Long-poll `getUpdates` by default; webhook optional.

### Message flow (inside Telegram extension)

1. **Poll loop** (`monitor.ts` → `monitor-polling.runtime.ts`) — `TelegramPollingSession` wraps `grammy` Runner, polls `getUpdates()` ~1s.
2. **Update handler** (`bot-handlers.runtime.ts: registerTelegramHandlers()`) — Looks up account → creates message context → normalizes target → builds inbound envelope → dispatches.
3. **Dispatch to agent** (`bot-message-dispatch.ts`) — Agent receives inbound, calls tools, tool results route to Telegram API.
4. **Outbound** (`send.ts: sendMessageTelegram()`) — Resolves API method (sendMessage, sendPhoto, etc.), formats payload (MarkdownV2/HTML + inline keyboard), fetches `api.telegram.org/bot<token>/<method>`, handles 429 backoff, handles media upload with `file_id` cache, returns `{messageId, timestamp, ...}`.

### Multi-account & threading

- Accounts stored in `~/.openclaw/credentials/telegram/<accountId>.json`.
- `thread-bindings.ts` maps forum topics → agent sub-threads with idle timeout + max-age enforcement.

### Polling vs webhook

- **Polling (default):** `grammy` Runner loops `getUpdates(offset)`; offset persisted.
- **Webhook (optional):** Express handler at `POST /telegram/:accountId`.

### Config schema

`extensions/telegram/src/config-schema.ts`:
```ts
TelegramChannelConfigSchema = {
  type: "object",
  properties: {
    accounts:   { type: "array", items: {...} },
    allowFrom:  { type: "array" },
    dmPolicy:   { enum: ["pairing", "open"] },
    group:      { ... },
    reaction:   { ... },
  }
}
```

### Public API facades

- `extensions/telegram/api.ts` — thin public surface (e.g., `inspectTelegramReadOnlyAccount()`, `collectTelegramUnmentionedGroupIds()`).
- `extensions/telegram/runtime-api.ts` — heavy, lazy-loaded: `setTelegramRuntime()` / `getTelegramRuntime()` for test injection.

### Key implementation details to preserve

1. **Lazy loading** — heavy modules (`send.ts`, `monitor-polling.runtime.ts`) imported at runtime only.
2. **Pluggable components** — custom `fetch`, throttler, processor injectable.
3. **Error handling** — network error classification, 429 exponential backoff, Telegram API errors surfaced as user messages.
4. **Text encoding** — MarkdownV2 or HTML; `format.ts` handles escape/entity wrap; inline keyboard as JSON.
5. **Media** — `file_id` cache to avoid re-upload; MIME sniff; async binary fetch.

---

## 8. Porting Risks & Non-Obvious Gotchas

### TS-specific patterns

- **Generics & discriminated unions** — `Result<T,E>`, `type: "success"|"error"` → Python `Literal`, `TypedDict`, `dataclass`, or Pydantic discriminated unions.
- **Zod** is code-first; port schema-by-schema to Pydantic models. Keep field names identical (snake_case vs camelCase decision: prefer snake_case in Python with explicit `alias=` for wire compat).

### Async/runtime

- **Event loop parity** — Node single-threaded; Python `asyncio` with `aiohttp` + `websockets`. Avoid threads unless unavoidable.
- **Dynamic imports** — openclaw uses `import("./foo.js")` for lazy boundaries. Python equivalent: `importlib.import_module()` or late-bound class instantiation. Prefer DI over ad-hoc dynamic import.
- **ESM re-exports** (`export { x } from "./y.js"`) → Python `__all__` + explicit imports.

### Plugin architecture

- **Manifest-first** — Control plane must load manifests WITHOUT executing plugin code. In Python, keep the manifest as a JSON file; load plugin code only when needed via `importlib`.
- **Plugin registry** — Global mutable registry. Use module-level dict or singleton class.
- **SDK isolation** — Plugins import only `oxenclaw.plugin_sdk.*`; core never imports `oxenclaw.extensions.*.internal.*`. Enforce with package layout + import linter (e.g., `import-linter`).

### Config & credentials

- YAML config → `PyYAML` + Pydantic. Env var substitution (`$TELEGRAM_BOT_TOKEN`) → resolve at load via `os.environ`.
- Credentials as JSON files at `~/.openclaw/credentials/...` (keep path for compat, or move to `~/.oxenclaw/` — **DECISION NEEDED**).

### Gateway protocol

- JSON-RPC over WebSocket → `FastAPI` + `websockets`. Validate params/results with Pydantic at the boundary.
- Event streaming reliability → `asyncio.Queue` backpressure; never drop frames.

### Message delivery

- **Chunking** — Telegram 4096 char limit; long replies split. Port `draft-stream.ts` logic verbatim.
- **Message editing** — cache sent message IDs for in-flight edits.

### Media

- **`file_id` caching** → dict or sqlite.
- **MIME detection** → `python-magic` or `filetype`.

### Polling

- `getUpdates` long-poll (~30s timeout) → `aiohttp` with timeout, `asyncio.sleep()` backoff, `tenacity` for retries.
- Graceful shutdown → `asyncio` signal handlers (`loop.add_signal_handler(SIGTERM, ...)`).

### Testing

- Mock Telegram API at HTTP boundary (`aioresponses` or `respx`).
- Update dedup by update ID → set + time-window cache.
- Per-account fixtures for isolation.

---

## Porting Approach — Phase Summary

**Phase D (THIS DOC) — complete.**

**Phase B — Telegram proof-of-concept:**

1. `oxenclaw/plugin_sdk/` — abstract channel contract, Pydantic config models, logger/runtime env.
2. `oxenclaw/gateway/` — WebSocket server, JSON-RPC protocol.
3. `oxenclaw/extensions/telegram/` — mirror TS file structure: `channel.py`, `bot_core.py`, `send.py`, `monitor.py`, …
4. `oxenclaw/cli/` — `typer`-based CLI skeleton.

**Phase A — Core expansion:** agents, canvas host, more channels.

**BR-1 — Browser tools (shipped 2026-04-26):** thin `oxenclaw/browser/` package wrapping Playwright with the existing `security/net/` SSRF/pinning/audit primitives. Closed-by-default `BrowserPolicy`, layered egress (URL preflight + per-request route + DNS rebind defense + dead proxy), `default_browser_tools` bundle of 5 always-safe tools. See [`BROWSER.md`](./BROWSER.md).

**CV-1 — Dashboard canvas (shipped 2026-04-26):** `oxenclaw/canvas/` package + `gateway/canvas_methods.py` + `tools_pkg/canvas.py` + dashboard SPA additions. Dashboard-only output via sandboxed `<iframe srcdoc>` — no native node, no Tailscale, no live-reload watcher, no external URL fetch. Empirically gated on `gemma4:latest` 25/25 before commit. See [`CANVAS.md`](./CANVAS.md).

**Dashboard SPA — `oxenclaw/static/` (vanilla JS, no build step):** the openclaw `ui/` Vue/TS app is out of scope, but oxenClaw ships its own minimal control plane that serves on the same port as the JSON-RPC websocket. 10 routes (chat, agents, channels, sessions, cron, approvals, skills, memory, config, rpc), light/dark theme toggle with system-preference detection, Ctrl+K command palette (14 actions), in-app login gate, sessions browser wired to `sessions.*` RPCs, dashboard chat image upload (📎 → 10 MiB cap → `data:image/...` URI). Responsive: < 900 px collapses the sidebar to a slide-in drawer. 23-test Playwright E2E suite under `tests/dashboard/` exercises every interactive surface and asserts no JS errors fired during the test.

**Anthropic agent (removed 2026-04-26):** the inline `AnthropicAgent` was deleted in favour of `PiAgent`'s richer Anthropic path (cache_control, thinking, cache observability, compaction, persistence). `--provider anthropic` is now a thin CLI alias of `pi` pinned to `claude-sonnet-4-6` by default; pass `--model` to override.

**vLLM provider (added 2026-04-26):** `--provider vllm` is a thin alias of `local` with strict-OpenAI payload (no Ollama-specific `num_predict`) and warmup off; defaults to `http://127.0.0.1:8000/v1`. See README "Internal vLLM server" section.

**Ollama native provider (added 2026-04-29):** `oxenclaw/pi/providers/ollama.py` posts to Ollama's native `/api/chat` instead of the OpenAI compatibility shim at `/v1/chat/completions`. The shim silently caps `options.num_ctx` at 4096, truncating memory + skill manifests so the model never sees the tool schemas; native honours the full options surface (`num_ctx` defaults to 16384, override via `OXENCLAW_OLLAMA_NUM_CTX`). Tool-using rounds run non-stream because native batches `tool_calls` into the final `done` frame anyway. Trace events flow through `OXENCLAW_LLM_TRACE` exactly like the OpenAI path.

**Guiding principles:**
- Preserve manifest-first plugin loading.
- Keep SDK contract in its own package.
- `asyncio` throughout.
- Pydantic parity with Zod.
- Mock-driven tests for every channel boundary.
