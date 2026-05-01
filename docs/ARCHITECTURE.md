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

**Ollama native provider (added 2026-04-29):** `oxenclaw/pi/providers/ollama.py` posts to Ollama's native `/api/chat` instead of the OpenAI compatibility shim at `/v1/chat/completions`. The shim silently caps `options.num_ctx` at 4096, truncating memory + skill manifests so the model never sees the tool schemas; native honours the full options surface. `num_ctx` defaults to 32768; `OXENCLAW_OLLAMA_NUM_CTX=auto` detects each model's max from `/api/show` and uses `min(model_max, 32768)` — auto only ever *lowers* num_ctx, never raises it, because cold-allocating a 65 K+ KV cache on a 16 GB machine pegs Ollama for minutes and starves concurrent embedding traffic. Bumping above 32 K is an explicit-integer-only operator decision. Tool-using rounds run non-stream because native batches `tool_calls` into the final `done` frame anyway. Trace events flow through `OXENCLAW_LLM_TRACE` exactly like the OpenAI path. Sizing guide: [`OLLAMA.md`](./OLLAMA.md).

**`llamacpp-direct` managed provider + new default (added 2026-04-29):** `oxenclaw/pi/providers/llamacpp_direct.py` + `oxenclaw/pi/llamacpp_server/` spawn and own a `llama-server` child process with the unsloth-studio fast preset (`--flash-attn on --jinja --no-context-shift -ngl 999 --parallel 1`). Live measurement on the same RTX 3050 + same Q4_K_XL gemma-4-E4B GGUF: 16.6 tok/s (`llamacpp-direct`) vs 5.6 tok/s (Ollama native), ~3× warm-decode speedup. The CLI default flipped from `--provider ollama` to `--provider auto`, which calls `agents.factory.resolve_default_local_provider()`: returns `"llamacpp-direct"` when `$OXENCLAW_LLAMACPP_GGUF` is set and a `llama-server` binary is reachable, else falls back to `"ollama"` so existing Ollama-only installs keep working unchanged. The legacy `local`/`pi` aliases now route through the same resolver. Embeddings are still served by Ollama (`nomic-embed-text`); both backends coexist on one host. Full guide: [`LLAMACPP_DIRECT.md`](./LLAMACPP_DIRECT.md).

**Guiding principles:**
- Preserve manifest-first plugin loading.
- Keep SDK contract in its own package.
- `asyncio` throughout.
- Pydantic parity with Zod.
- Mock-driven tests for every channel boundary.

---

## Run-loop reliability hardening (2026-04-30)

A 17-item hardening pass tightened the `pi/run` loop. Some items
match openclaw upstream constants exactly; others are oxenClaw
additions inspired by openclaw's overall stance but without a direct
upstream equivalent. Each entry below labels the lineage explicitly.

**Defaults**
- `RuntimeConfig.max_tool_iterations` 8 → **25**
  (oxenClaw choice — no single upstream equivalent; tuned for
  multi-hop chains under small local models).
- `RuntimeConfig.unknown_tool_threshold` 3 → **10** (matches openclaw
  `UNKNOWN_TOOL_THRESHOLD` in `src/agents/tool-loop-detection.ts`).
  Combined with one-shot tool-list reinjection.
- `RuntimeConfig.max_compression_self_heals` 2 → **3**
  (oxenClaw choice — no upstream named constant).
- New `RuntimeConfig.arg_loop_threshold=4` — oxenClaw addition: same
  `(name, args_digest)` repeated N times in a row triggers a
  `loop_detection` abort even when each call individually succeeded.
  Targets the "0-hit web_search re-emit" symptom on small models.

**Robustness**
- `attempt.py`: `tool_buf` race guard — `input_delta` arriving before
  `tool_use_start` no longer surfaces a nameless tool; falls back to
  `_parse_error` so the model self-corrects.
- `pi_agent._maybe_auto_fire_pseudo_tool`: pseudo-tool autofire now
  iterates up to **3 rounds per turn**. (oxenClaw addition — openclaw
  doesn't have a textual-pseudo-call autofire path; this exists
  because small local models routinely emit tool calls as JSON in
  reply text instead of real `tool_use` blocks.)
- `_maybe_rotate_credential`: drops the `api_key[:8]` guess fallback —
  log + skip when `current_key_id` is unavailable. Matches the
  general stance "don't cool the wrong key" rather than a specific
  upstream call site.

**Model-aware estimators (oxenClaw additions)**
- New `oxenclaw/pi/run/token_estimator.py`: family-aware chars/token
  ratios (anthropic 3.0, qwen 2.5, gemma 2.8, llama 3.0, default 3.5)
  with optional `tiktoken` passthrough for OpenAI models. openclaw
  uses a single `ESTIMATED_CHARS_PER_TOKEN = 4` constant
  (`preemptive-compaction.ts`); we deliberately diverge to better
  fit Korean qwen / gemma sessions. `preemptive_compaction.decide()`
  now consumes the per-model ratio.
- New `vision_keep_turns_for(model_id)` in `history_image_prune.py`:
  claude=6, gpt-4o=6, qwen/gemma=4, llava=3, default=2. openclaw's
  retention is token-budget based (`keepRecentTokens`); ours is
  user-turn based — a deliberate simplification.

**Policy & engine wiring**
- `RuntimeConfig.tool_policy` is now applied at the run-loop entry:
  `policy.resolve(tools)` filters disabled / denied tools out of the
  model's view, and `policy.max_chars_for(name)` drives per-tool
  result truncation. Previously the operator had to wire it
  themselves.
- `pi/context_engine/openclaw_engine.py` (new): `OpenclawContextEngine`
  is now the **PiAgent default**. Despite the name, this is an
  oxenClaw original; the name reflects "openclaw-style eager trim"
  rather than a direct port. Subclasses `LegacyContextEngine` and
  overrides `assemble()` to proactively trim `ToolResultBlock`
  bodies when the running token estimate crosses 80 % of the budget.
  Below that threshold it's a no-op so legacy callers keep their
  byte-for-byte behaviour. Operators wanting strict pre-rc.16
  behaviour can inject `LegacyContextEngine()` explicitly.

**Failover (oxenClaw addition)**
- `RuntimeConfig.failover_cycle: bool=False` (opt-in). When True,
  the chain wraps from tail back to head once it's exhausted; bounded
  by `failover_cycles_used >= len(chain)` so a permanently-broken
  set of models can't loop forever. `should_failover` /
  `resolve_next_model` accept `cycle` + `cycles_used` and the run
  loop tracks `failover_cycles_used`, incrementing on tail→head
  wrap. openclaw's failover-policy is single-pass with no equivalent
  cycle option — this is an oxenClaw-only knob.

**Concurrency hygiene**
- `LaneRegistry`: drop `asyncio.Semaphore._value` private-attr
  access; track `_in_flight_count` via `try/finally` so `stats()`
  remains accurate across asyncio versions.

**Loop detection**
- New `oxenclaw/pi/run/arg_loop_detector.py`: `ArgLoopDetector`
  keeps a SHA1-digested `(name, args_digest)` deque (length 16,
  threshold 4). Wired into the run loop's tool-result phase.
  oxenClaw addition — openclaw's `tool-loop-detection.ts` has a
  conceptually similar `genericRepeat` detector but a different
  shape; we did not port it 1:1.
- Unknown-tool abort path now sends a tool-list reinjection nudge
  once before the structural `loop_detection` stop_reason. (oxenClaw
  addition; not a 1:1 port.)

**Tests**: 2064 passed (8 new modules, 1 updated assertion).
**Lint**: ruff strict + pyright strict clean across all touched files.


## Length-cutoff recovery + model-aware num_predict (2026-05-01)

Follow-up to the 2026-04-30 reliability pass. Thinking-class models
(qwen3.5, deepseek-r1) frequently spend their entire `num_predict`
allotment on hidden tokens and emit `stop_reason="length"` with no
visible text — the user sees an empty reply that previously terminated
the turn.

**`oxenclaw/pi/run/stop_recovery.py`** — new `is_length_truncation()`
helper: `stop_reason == "length"` AND no visible text AND no
`tool_use` blocks. Tool-use turns are excluded so the normal tool-loop
path stays in charge of mid-chain length stops.

**`oxenclaw/pi/run/attempt.py`** — `default_max_tokens_for(model)`
chooses the per-attempt `num_predict` when nothing is pinned: thinking
models get `min(max_output_tokens, 4096)` (≈4× plain), non-thinking
get `min(max_output_tokens, 1024)`. `run_attempt(...)` accepts a new
`max_tokens_override: int | None` parameter so the run loop can pass a
bumped value on retry without mutating `RuntimeConfig`.

**`oxenclaw/pi/run/runtime.py`** — two new knobs:
- `length_recovery_attempts: int = 1` (set 0 to disable)
- `length_recovery_growth: float = 2.0` (multiplier per retry, capped at
  `model.max_output_tokens`)

**`oxenclaw/pi/run/run.py`** — when `is_length_truncation(msg)` fires,
the loop pops the noise turn from `appended` / `working` (zero-content
message, no value to retain), bumps `length_max_tokens_override`, and
re-enters the attempt loop. No synthetic user nudge — the fix is
structural, not content-shaped.

Live verification: cap=100 / grow=10× on `qwen3.5:9b` reproduces the
empty-length first attempt → 100 → 1000 bump → `'OK'` returned in 47s
(`elapsed=47.1s, attempts=2, stop=stop`). Multi-turn live suite
(memory_recall + tool_roundtrip + multi_fact) passes 3/3 with the new
defaults.

This is an oxenClaw addition; openclaw upstream doesn't expose an
equivalent length-recovery knob.

**Tests**: `tests/test_pi_stop_recovery.py` adds 4 cases (predicate +
default helper + run-loop recovery + disabled path); 12/12 pass.
118 PI run-loop suite tests pass.


## Overload-class failover backoff (2026-05-01)

Audit against openclaw upstream (commit `3f7f2c8dc9`, 2026-05) found
that `should_failover` (`failover.py:82-98`) returned
`failover=True` for any retryable provider error reaching the
post-classification branch. A single 429 burst could rotate the chain
head before the active model had a chance to recover, defeating the
point of having a chain at all (the next provider hits the same
saturated gateway).

**`oxenclaw/pi/run/run.py`** — port of `OVERLOAD_FAILOVER_BACKOFF_POLICY`
(`pi-embedded-runner/run.ts:100-105`):
- `_OVERLOAD_FAILOVER_INITIAL_MS = 250`, `MAX_MS = 1500`, `FACTOR = 2`,
  `JITTER = 0.2`. Exact match with upstream.
- New `_overload_failover_backoff_seconds(attempt)` reuses the existing
  decorrelated-RNG path (`_RETRY_COUNTER` + `_RETRY_COUNTER_LOCK`) so
  concurrent run loops don't sync up under load.
- `overload_failover_attempts` counter on the run loop, incremented
  per chain walk caused by `RATE_LIMIT` / `SERVER` reasons. Walk delay
  scales 250 → 500 → 1000 → 1500 → 1500ms with ±20% jitter.
- Non-overload reasons (`AUTH`, `MODEL_NOT_FOUND`, ...) still rotate
  immediately — pacing only applies where the next provider is likely
  to be saturated by the same upstream condition.

This is a faithful port. Upstream calls
`maybeBackoffBeforeOverloadFailover(reason)` at four sites
(`run.ts:1420, 1440, 1552, 1557`) covering profile rotation and
fallback model switch; oxenclaw consolidates this at the single
failover-decide site since the run loop's structure is flatter.

**Tests**: `tests/test_failover.py` adds 2 cases (helper bounds + a
fake-provider integration test that patches `asyncio.sleep` and asserts
the paced delay fires before the chain walk); 9/9 pass. Total PI
run-loop suite: 120 pass.


## Session-expired classification + context-window guard (2026-05-01)

Two more upstream-parity ports surfaced by the openclaw audit
(`3f7f2c8dc9`):

**HTTP 410 → `SESSION_EXPIRED` (failover trigger)**
- Previously oxenclaw mapped 410 to `UNKNOWN` non-retryable, terminating
  long-running sessions that hit provider session expiry (Cloud Code
  Assist, hosted IDE backends after long idle).
- New `FailoverReason.SESSION_EXPIRED` in `error_classifier.py:54-60`,
  routed from status_code 410. Mirrors openclaw `failover-error.ts:64`.
- The classifier marks it `retryable=False, should_fallback=True` so
  the run loop walks the chain instead of dead-ending.

**Context-window pre-flight guard**
- New `oxenclaw/pi/run/context_window_guard.py` ports
  `agents/context-window-guard.ts:1-74`. Constants pinned identical to
  upstream: `CONTEXT_WINDOW_HARD_MIN_TOKENS=16_000`,
  `CONTEXT_WINDOW_WARN_BELOW_TOKENS=32_000`.
- Exposes `evaluate_context_window_guard(tokens)` (returns flags) and
  `assert_context_window_usable(model_id, tokens)` (raises
  `ContextWindowTooSmallError` below hard min).
- PiAgent `__init__` calls the *warning-only* path so misconfigured
  tiny windows surface in logs without breaking the catalog's
  intentionally-small entries (e.g. `gemma3:4b` at 8k). Operators who
  want hard-fail behaviour invoke `assert_context_window_usable`
  directly.

**Tests**:
- `tests/test_error_classifier.py` adds 1 case (410 → SESSION_EXPIRED).
- `tests/test_context_window_guard.py` adds 7 cases (warn / block /
  clean / unknown-window / raise / threshold pin).
- Total PI run-loop suite: 152 pass.


## Replay sanitiser + compaction aggregate timeout (2026-05-01)

Two more upstream-parity ports (openclaw `3f7f2c8dc9`).

**Replay tool-call sanitiser**
- New `oxenclaw/pi/run/replay_sanitizer.py` ports
  `pi-embedded-runner/run/attempt.ts:649-848` (commit `c3972982b5`).
  When a session is reloaded from disk (SQLite, ACP transcript) the
  persisted `ToolUseBlock`s may have been written mid-stream by a prior
  crashed run. The sanitiser walks each AssistantMessage and drops
  blocks with empty `id`, missing `input`, whitespace/overlong/
  unregistered `name`. AssistantMessages with no surviving content are
  removed entirely.
- Wired into PiAgent `_ensure_session` right after the existing
  `repair_incomplete_turn` step, so a corrupt-on-disk transcript can't
  ride into the next turn and 400 the provider.

**Compaction aggregate timeout**
- New `oxenclaw/pi/run/compaction_timeout.py` ports
  `pi-embedded-runner/run/compaction-retry-aggregate-timeout.ts`. A
  stuck summariser (auxiliary LLM hung mid-stream, network black hole)
  could otherwise park the entire run forever — preemptive compaction
  exists for exactly this fallback, but only if we let go of the stuck
  call first.
- `with_compaction_timeout(coro, timeout_seconds=...)` wraps the
  coroutine in `asyncio.wait_for`, swallows `TimeoutError` (returns
  None), invokes an optional `on_timeout` callback, and propagates any
  inner exception unchanged.
- New `RuntimeConfig.compaction_timeout_seconds: float | None = 120.0`
  knob. PiAgent wraps `engine.compact(...)` with the helper so a
  default 120s ceiling kicks in without operator action.

**Tests**:
- `tests/test_replay_sanitizer.py` adds 8 cases (well-formed
  passthrough, drop-empty-id, drop-unregistered-name,
  drop-whitespace-name, drop-overlong-name, no-allowlist passthrough,
  partial-survival, idempotency).
- `tests/test_compaction_timeout.py` adds 7 cases (passthrough, None /
  zero / negative disable, timeout returns None, on_timeout callback,
  callback exception swallowed, inner exception propagates).
- Total PI run-loop suite: 167 pass.


## Smart per-tool-result truncation, context-window aware (2026-05-01)

Final batch of openclaw-parity ports. The architect's audit flagged two
gaps in oxenclaw's tool-result handling: (1) the per-tool truncator was
opt-in via `EffectiveToolPolicy` so most callers got no clipping at
all, and (2) when policy *was* configured, the head-only
`[...truncated N chars]` strategy lost error/JSON/summary content at
the tail.

**`oxenclaw/pi/run/tool_result_truncation.py`** — port of
`pi-embedded-runner/tool-result-truncation.ts:1-360`. Pinned constants:
- `MAX_TOOL_RESULT_CONTEXT_SHARE = 0.3`
- `HARD_MAX_TOOL_RESULT_CHARS = 400_000`
- `MIN_KEEP_CHARS = 2_000`
- `_CHARS_PER_TOKEN = 4`

Public API:
- `truncate_tool_result_text(text, max_chars)` — head+tail aware
  string truncator. When the trailing ~2K chars match
  `error|exception|failed|...` keywords or close a JSON object, the
  function reserves `min(budget // 3, 4_000)` for the tail and cuts
  the head, with a `MIDDLE_OMISSION_MARKER` between. Otherwise keeps
  the head and clips at the nearest newline boundary above 80 % of
  the budget. Operator-supplied tight caps (e.g. `max_result_chars=200`)
  override the `MIN_KEEP_CHARS` floor.
- `calculate_max_tool_result_chars(context_window_tokens)` — resolves
  per-result char budget from the model's context window: 30 % share,
  capped at 400K chars (≈100K tokens). 32K-window → 38_400 chars;
  200K-window → 240_000 chars; 2M-window → 400_000.
- `truncate_tool_result_message(msg, max_chars)` — operates on a
  `ToolResultMessage`, distributes budget proportionally across text
  blocks for multi-text tool outputs.
- `truncate_oversized_tool_results_in_messages(messages, ...)` — bulk
  in-place pass; idempotent, safe to call every turn.
- `session_likely_has_oversized_tool_results(messages, ...)` — fast
  detection so the compress-then-retry path can skip work when nothing
  needs trimming.

**Run-loop wiring** — `run.py` now applies a *two-tier* per-tool cap
unconditionally on every tool result, before it lands in
`ToolResultMessage`:
1. `min(EffectiveToolPolicy.max_chars_for(name), ctx_share_cap)` —
   policy override (when set) AND context-share cap (always).
2. The smart head+tail truncator handles the actual trim.

This serves the same role as openclaw's
`tool-result-context-guard.ts` runtime guard: oversize tool output
never enters history. The more elaborate cumulative
`compactExistingToolResultsInPlace` path is left to oxenclaw's
existing `preemptive_compaction` / `OpenclawContextEngine`.

**Real-world impact**: a `web_fetch` returning 50 KB of HTML against
`gemma3:4b` (8K context window) used to flow through unclipped if no
`tool_policy` was configured (most call paths). It now auto-clips to
~9.6 KB (30 % × 8K × 4 chars/tok = 9_600). For `qwen3.5:9b` (256K
context) the same fetch passes through cleanly — both ends correct.

**Tests**: `tests/test_tool_result_truncation.py` adds 13 cases
(passthrough, head-only newline boundary, head+tail on error keyword,
head+tail on JSON close, all four budget calculations including hard
cap and floor, single-string message, multi-block message, session
under-budget skip, session over-budget trim, idempotency, fast-detect
predicate). Existing `test_run_loop_tool_policy.py` updated to
accommodate the new informative truncation suffix. Total PI run-loop
suite: 180 pass.


## Local-LLM operability batch (2026-05-01)

Architect audit focused on the project goal — running the openclaw
agent loop on local models (qwen3.5, gemma4, llama3.1 via Ollama;
direct llama.cpp HTTP) — surfaced six discrete gaps. Two passes landed
together.

**Pass A — Ollama / shim hygiene (4 fixes)**

- `oxenclaw/pi/providers/ollama.py:288-310` — `keep_alive` defaults to
  `"30m"` (override via `OXENCLAW_OLLAMA_KEEP_ALIVE`). Ollama's own
  default is `5m`; long agent turns (compaction + tool round-trips +
  paced backoff) routinely exceed that and the model gets unloaded
  mid-session, paying a 30-90s cold-load penalty that classifies as a
  transport timeout.
- `oxenclaw/pi/providers/ollama.py:455-465` — dropped the
  `if ctx.tools: nonstream` short-circuit. Native streaming already
  surfaces `tool_calls` from the `done` frame as synthesised
  Start/InputDelta/End events, so streaming on tool-using rounds keeps
  `text_emitted` accurate and gives downstream UIs a heartbeat instead
  of a multi-second freeze.
- `oxenclaw/pi/providers/ollama.py:241-265` — pure-text
  `ToolResultBlock.content` is now serialised as a flat newline-joined
  string for the `tool` role. The JSON-envelope shape
  (`[{"type":"text","text":"..."}]`) is reserved for genuinely-mixed
  (text + image) results. 7-13B local models otherwise mistake the
  envelope as the tool's return shape and re-wrap on the next turn,
  burning tokens and ignoring the actual content.
- `oxenclaw/pi/run/attempt.py:128-150` — gate
  `cache_control_breakpoints` on `Model.supports_prompt_cache`. Cache
  markers are an Anthropic-specific feature; emitting them on Ollama,
  llama.cpp, or vLLM is at best ignored and at worst breaks `--jinja`
  template rendering.

**Pass B — Thinking-model recovery (qwen3.5 / deepseek-r1)**

- `oxenclaw/pi/run/stop_recovery.py:57-89` — `is_length_truncation`
  now also fires for `stop_reason in {None, "stop", "end_turn"}` when
  the assistant turn has a `ThinkingBlock` but no visible text and no
  `tool_use`. This is the operator-visible "thought without
  responding" failure: the model finished thinking under-budget and
  decided it was done. The fix is structural (bump `max_tokens` and
  retry), reusing the existing length-recovery branch in `run.py`.
- `oxenclaw/pi/run/attempt.py:56-83` — `_split_thinking_tags(text)`
  strips `<think>...</think>` (case-insensitive, multiline) from the
  assembled visible text and preserves the captured thinking on the
  `ThinkingBlock`. Some llama.cpp / vLLM builds leak think tags into
  the visible stream when reasoning-format support is off; without the
  strip those leaked tags would block the thinking-only-stop classifier
  and the user would see raw `<think>...` in the channel.

**Tests**: `tests/test_pi_providers.py` adds 3 cases (keep_alive default,
env override, tool_result flat-string). New
`tests/test_pi_attempt_cache_gate.py` adds 3 cases (gate off / on /
operator-disabled). `tests/test_pi_stop_recovery.py` adds 2 cases
(thinking-only natural stop, `_split_thinking_tags` correctness). Total
PI run-loop suite: **194 pass**.

Deferred from the audit: tool-call delta race for vLLM parallel tools
(M, our deployments don't run vLLM with parallel tool calls today),
provider-specific `stop_reason` normalisation (S, llama.cpp-direct only,
better as a hotfix when observed), shared-Ollama embed contention (M,
hardware-bound), CodingAgent / LocalAgent pseudo-tool autofire (M,
those agents have their own tool flow), system-prompt shrink for tiny
models (M, design choice — auto vs opt-in).
