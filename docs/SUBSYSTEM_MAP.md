# openclaw ↔ oxenClaw subsystem map

Scope re-confirmation before continuing the port. Rough LOC column = non-test `.ts` file count in `openclaw/src/<dir>/` (transitive, max-depth 3). Extensions are counted separately.

## Legend

- **Status** — In: has a Python counterpart under `oxenclaw/`. Partial: started but incomplete. Out: intentionally not ported. TBD: undecided.
- **Priority** — P0 required for Phase B exit. P1 needed for Phase A. P2 defer. Out = never.

## `openclaw/src/*` → `oxenclaw/*`

| openclaw dir | ts files | oxenclaw | Status | Priority | Notes |
|---|--:|---|---|---|---|
| `plugin-sdk/` | 365 | `plugin_sdk/` | **Partial** | P0 | 8 modules ported (channel_contract, config_schema, config_runtime, runtime_env, reply_runtime, media_runtime, error_runtime) vs 365 upstream files — mostly types/helpers; revisit what must mirror vs what we can trim |
| `gateway/` | 328 | `gateway/` | **Partial** | P0 | server + router + 9 method modules (chat, cron, agents, approval, config, channels, isolation, memory, skills); no event framing, no pairing, no trajectory |
| `plugins/` | 241 | `plugins/` | **Partial** | P0 | manifest + discovery + registry ported; no activation boundary, no lifecycle |
| `channels/` | 155 | `channels/` | **Partial** | P0 | router + runner only |
| `agents/` | 763 | `agents/` | **Partial** | P0 | echo + local + anthropic agent, history, tools, factory, dispatch, registry, base — tool registry + provider SDK only stub-grade |
| `config/` | 187 | `config/` | **In** | P0 | loader + env_subst + paths + credentials |
| `cli/` | 229 | `cli/` | **Partial** | P0 | gateway/message/memory/skills/config commands; wizard/setup flows absent |
| `cron/` | 78 | `cron/` | **In** | P0 | scheduler + store + trigger + models |
| `security/` | 35 | `security/` | **In** | P0 | shell_tool, isolated_function, tool_runner, skill_scanner |
| `memory/` | 1 | `memory/` | **In** | P1 | thin; main logic lives in `memory-host-sdk/` |
| `memory-host-sdk/` | 57 | — | **Out** | P1 | retriever + embeddings + store partially covered by `oxenclaw/memory/`; revisit once A.1 lands |
| `tasks/` | 35 | — | **TBD** | P1 | gateway task tracking |
| `commands/` | 325 | — (absorbed by `cli/` + `gateway/chat_methods`) | **Partial** | P1 | native-command registry is per-extension in openclaw; decide global vs extension |
| `auto-reply/` | 306 | — | **TBD** | P1 | substantial — agent-driven auto-replies; may be partially covered by `agents/` |
| `mcp/` | 8 | `pi/mcp/` | **Partial (M1: client only)** | P2 | MCP client phase shipped 2026-04-25 — stdio + HTTP/SSE transports, name dedup, factory wiring via `load_mcp_tools()`. M2 (oxenClaw as MCP server) not yet started. Worked example: `docs/MCP_YAHOO_FINANCE.md`. |
| `hooks/` | 34 | — | **Out** | P2 | Claude Code hook integration, JS-specific |
| `secrets/` | 63 | — | **TBD** | P1 | credential encryption layer; currently plaintext in `config/credentials.py` |
| `shared/` | 63 | — | **Partial** | P1 | utility grab-bag; port on demand |
| `sessions/` | 12 | — | **TBD** | P1 | session lifecycle tracking |
| `logging/` | 30 | — (stdlib `logging` inline) | **Out** | P2 | OTel + structured logs; stdlib is enough for now |
| `acp/` | 40 | — | **Out** | P2 | Anthropic Console Protocol client — specialty |
| `daemon/` | 37 | — | **TBD** | P1 | background process supervision |
| `tui/` | 34 | — | **Out** | P2 | Node Ink UI |
| `process/` | 20 | — | **TBD** | P1 | child process management (agent sandbox) |
| `terminal/` | 14 | — | **Out** | P2 | PTY/terminal integration |
| `routing/` | 9 | — | **TBD** | P1 | internal message routing |
| `bootstrap/` | 2 | — | **Out** | P2 | Node-specific entry setup |
| `compat/` | 1 | — | **Out** | Out | legacy Node compat shims |
| `bindings/` | 1 | — | **Out** | Out | native bindings |
| `node-host/` | 10 | — | **Out** | Out | in-Node plugin host — replaced by Python importlib |
| `canvas-host/` | 3 | `canvas/` + `gateway/canvas_methods.py` + `tools_pkg/canvas.py` + `skills/canvas/` | **Partial (CV-1, dashboard-only)** | P2 | Dashboard-embedded canvas; no Tailscale bridge / a2ui / native node. Empirically gated on gemma4:latest 25/25. See docs/CANVAS.md |
| `context-engine/` | 6 | — | **TBD** | P2 | prompt context assembly |
| `link-understanding/` | 6 | — | **Out** | P2 | URL preview/analysis |
| `markdown/` | 7 | — | **Out** | P2 | MD rendering helpers |
| `media/`, `media-generation/`, `media-understanding/`, `image-generation/`, `video-generation/`, `music-generation/`, `realtime-voice/`, `realtime-transcription/`, `tts/` | 119 total | — | **Out (for now)** | P2 | multi-modal providers — no Phase A commitment |
| `web/`, `web-fetch/`, `web-search/`, `proxy-capture/` | 14 total | — | **Out** | P2 | agent tools; port as agent tool plugins later |
| `extensions/browser/` | 156 (~24K LOC) | `browser/` + `tools_pkg/browser.py` + `skills/browser/` | **Partial (BR-1)** | P2 | Headless Chromium via Playwright; egress fail-closed via NetPolicy + page.route + DNS pinning + dead proxy. CDP bridge / chrome-mcp / qa-lab dropped — see docs/BROWSER.md |
| `extensions/slack/` (upstream) | (varies) | `extensions/slack/` (oxenClaw) | **Partial (outbound-only)** | P2 | `chat.postMessage` only — Enterprise Grid friendly, supports per-account corp proxy `base_url`, retries 429 with `Retry-After`. Marked `outbound_only=True` so the monitor supervisor skips spawning a polling task. Inbound (Events API / Socket Mode) intentionally out of scope — see docs/SLACK.md |
| `flows/` | 11 | — | **Out** | P2 | preset conversation flows |
| `status/` | 8 | — | **TBD** | P2 | gateway status endpoints (partially in `gateway/`) |
| `pairing/` | 8 | — | **Out** | P2 | device pairing (mobile-only) |
| `wizard/` | 11 | — | **Out** | P2 | setup wizard — CLI-based lighter replacement in `cli/` |
| `types/` | 11 | — | **Out** | Out | shared TS types — Python uses Pydantic models in place |
| `utils/` | 26 | — | **Partial** | P1 | port on demand |
| `trajectory/` | 4 | — | **TBD** | P2 | agent run trajectory export |
| `chat/` | 2 | (in `gateway/chat_methods.py`) | **In** | P0 | |
| `polls/` | 0 (top-level files) | — | **Out** | P2 | Telegram polls — lives in extension |
| `interactive/`, `i18n/`, `library/` | 1/0/1 | — | **Out** | P2 | |

## `openclaw/extensions/*` → `oxenclaw/extensions/*`

openclaw ships **~90 extensions**. oxenClaw ports **1** (telegram) as pilot. Full list is parked for post-Phase-B scoping.

| Category | openclaw extensions | oxenClaw |
|---|---|---|
| **Pilot** | telegram | **telegram/** (Partial — 11 modules vs ~180 upstream) |
| Messaging channels | discord, slack, matrix, msteams, signal, whatsapp, line, imessage, bluebubbles, feishu, googlechat, google-meet, irc, mattermost, nextcloud-talk, nostr, qa-channel, qqbot, synology-chat, tlon, twitch, voice-call, webhooks, xiaomi, zalo, zalouser, phone-control | — |
| LLM providers | anthropic, anthropic-vertex, openai, google, amazon-bedrock, amazon-bedrock-mantle, alibaba, arcee, byteplus, chutes, cloudflare-ai-gateway, codex, copilot-proxy, deepgram, deepseek, elevenlabs, exa, fal, fireworks, github-copilot, groq, huggingface, kilocode, kimi-coding, lmstudio, mistral, moonshot, nvidia, ollama, openrouter, perplexity, qianfan, qwen, sglang, stepfun, synthetic, tencent, together, tokenjuice, vercel-ai-gateway, venice, vllm, volcengine, voyage, xai, zai, vydra, minimax, microsoft, microsoft-foundry, brave, searxng, tavily, firecrawl, duckduckgo, litellm, runway, comfy | All hosted providers (incl. Anthropic) go through `pi/`. `--provider anthropic` is a thin alias of `pi` pinned to a Claude default model. |
| Memory | active-memory, memory-core, memory-lancedb, memory-wiki | rolled into `oxenclaw/memory/` (monolithic, not pluggable yet) |
| Media cores | image-generation-core, media-understanding-core, speech-core, video-generation-core, talk-voice | — |
| Infra | acpx, bonjour, device-pair, diagnostics-otel, openshell, skill-workshop, thread-ownership | — |
| Dev/test | diffs, qa-lab, qa-matrix, qa-channel, test-support, llm-task, open-prose, lobster | — |

## Implication for scope

1. **Phase B exit (telegram working)** still needs only `P0` rows + `extensions/telegram/` — the current layout is correct.
2. **Extensions are plug-ins, not branches of the core.** Extension parity is a per-extension decision, not a core-port task. Skip the ~85 provider extensions for oxenClaw v1 unless user specifically wants one.
3. **`agents/` (763 files) is the largest single risk.** Most of openclaw's intelligence lives here. Phase A.1 ("real agent harness") will likely consume 50%+ of Phase A budget.
4. **`commands/` + `auto-reply/` (631 files combined)** overlap with `agents/` and `gateway/chat_methods`. Don't double-port — decide in a Phase A.1 ADR which layer each concept lives in on the Python side.

## Files to maintain next

- This document: update `Status` column as modules land.
- `docs/PORTING_PLAN.md`: update Phase A.* when this map narrows the scope.
- `docs/TELEGRAM_PARITY.md` (to be written): file-level parity for telegram only.
