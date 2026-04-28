# oxenClaw

[![ci](https://github.com/andreason21/oxenClaw/actions/workflows/ci.yml/badge.svg)](https://github.com/andreason21/oxenClaw/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A self-hosted, in-house AI assistant gateway in Python. Bring your
own model (local Ollama, Anthropic, or 22 providers via the bundled `pi`
runner), talk to it from the bundled web dashboard or the native
desktop app (Windows / Ubuntu), give it tools and skills, and let it
push outbound alerts to Slack — all on a long-lived service with
production-grade observability.

> Python port of [openclaw](https://github.com/openclaw/openclaw) — the
> server/CLI surface, hardened and documented for self-hosting.

[**한국어 README ↓**](#한국어)

---

## Why oxenClaw?

| | |
|---|---|
| 🦙 **Bring your own model** | Local Ollama by default (any tool-capable model). Anthropic, OpenAI-compatible, Bedrock, Google, Groq, DeepSeek, Mistral, Together, Fireworks, … 22 providers via `pi`. |
| 🖼️ **Multimodal in/out of the box** | Attach a photo in the dashboard chat (📎 button) or the desktop client and a vision-capable model (gemma4 / Claude 3+ / GPT-4o / Gemini 1.5+ / llava / etc.) sees it. Models without vision get a dropped-image notice in their text context. |
| 🖥️ **Bundled dashboard SPA** | Light/dark theme toggle, Ctrl+K command palette, sessions browser (list/preview/reset/fork/archive), responsive mobile drawer, in-app login gate. No build step, served on the same port as the JSON-RPC websocket. |
| 💻 **Native desktop app (Windows + Ubuntu)** | Tauri client for Windows 11 (`.msi` / NSIS `.exe`), Ubuntu 22.04 + 24.04 (`.deb`), or any glibc Linux (`.AppImage`). OS keychain–backed tokens, native toast notifications, system tray, Origin-locked WS upgrade, Ed25519-signed auto-updates. See [`docs/DESKTOP_APP.md`](docs/DESKTOP_APP.md). |
| 🔌 **Open by design** | Plugin SDK + entry-point discovery. New channels and skills install with `pip install`. |
| 🛡️ **Production-grade security** | NetPolicy + DNS pinning + SSRF guards, sandboxed tool execution (RLIMIT + bwrap), human-in-the-loop approval gating, dangerous-env stripping for subprocess MCP servers. |
| 📊 **Operationally serious** | Prometheus `/metrics`, `/healthz` + `/readyz`, structured JSON logs with per-RPC `trace_id`, graceful SIGTERM drain, online SQLite backup/restore. |
| 🧠 **Memory built-in** | sqlite-vec vector store + FTS5 + MMR rerank + embedding cache. Sessions persist with WAL; auto-compaction; durable knowledge base ("memory wiki"). |
| 🛠️ **Easy to extend** | Two files (`SKILL.md` + a Python tool) and your tool is in. Or import any existing MCP server via `mcp.json`. |
| 🪞 **No cloud lock-in** | Runs on a laptop, Pi, or systemd unit. All state under `~/.oxenclaw/`. |

---

## Install

> **On Windows?** Use the dedicated WSL2 guide:
> [`docs/INSTALL_WSL.md`](docs/INSTALL_WSL.md). Native Win32 is not
> supported (sandbox + signal + Linux networking dependencies).

```bash
# Clone and install in editable mode
git clone https://github.com/andreason21/oxenClaw.git
cd oxenClaw
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Sanity check
oxenclaw paths
oxenclaw config validate
```

**Linux / macOS / WSL2** are supported. Requires Python **3.11+**. The
default LLM backend is [Ollama](https://ollama.ai/)
running on `127.0.0.1:11434` — install Ollama and pull a tool-capable
model, e.g.:

```bash
ollama pull qwen3.5:9b
```

Override the model with `--model <id>`. Tested models:

| Model | Context | Notes |
|---|---|---|
| **`qwen3.5:9b`** *(default)* | 256K | Multimodal (vision), native function calling + thinking, ~6.6 GB Q4_K_M. Live e2e gate 18/18 PASS. |
| `gemma4:latest` (= `e4b`) | 128K | Multimodal (text+image), native function calling, ~9.6 GB. |
| `gemma4:e2b` | 128K | Lighter (~7.2 GB) — same family, smaller. |
| `gemma4:26b` / `31b` | 256K | Heavier MoE variants when you have the RAM. |
| `qwen2.5:7b-instruct` | 32K | Strong tool calling. |
| `llama3.1:8b` | 128K | Broadly capable. |
| `mistral-nemo:12b` | 128K | Slower, more verbose. |

You can run oxenClaw with no LLM (RPC + tools only) by using
`--provider echo` for testing.

#### Internal vLLM server

If your team runs an internal vLLM (`vllm serve …`) box, point the
gateway at it directly — no Ollama required:

```bash
oxenclaw gateway start \
    --provider vllm \
    --base-url http://internal-vllm.lan:8000/v1 \
    --model meta-llama/Llama-3.1-8B-Instruct \
    --api-key "$VLLM_API_KEY"   # only if vLLM was started with --api-key
```

`--provider vllm` is a strict-OpenAI-shape variant of `local` (no Ollama
extras like `num_predict`, no warmup ping). `--base-url` defaults to
`http://127.0.0.1:8000/v1`. `--api-key` is optional — pass it when vLLM
was started with its own `--api-key`. The same shape works in
`config.yaml`:

```yaml
agents:
  default:
    provider: vllm
    model: meta-llama/Llama-3.1-8B-Instruct
    base_url: http://internal-vllm.lan:8000/v1
    api_key: ${VLLM_API_KEY}
```

---

## Quick start

### 1. Minimum config

Create `~/.oxenclaw/config.yaml`:

```yaml
channels: {}     # populate per channel below
agents:
  default:
    id: default
    provider: local              # local | vllm | anthropic | pi | echo
    model: qwen3.5:9b
    system_prompt: |
      You are a helpful assistant.
```

### 2. Optional — wire Slack outbound alerts

Slack is the only outbound integration. Drop a workspace bot token at
`~/.oxenclaw/credentials/slack/main.json`:

```json
{ "token": "xoxb-..." }
```

Add the binding to `config.yaml`:

```yaml
channels:
  slack:
    accounts:
      - account_id: main
        display_name: "Workspace Alerts"
```

### 3. Start the gateway

```bash
export OXENCLAW_GATEWAY_TOKEN=$(openssl rand -hex 32)
oxenclaw gateway start --provider local
```

The gateway binds to `127.0.0.1` only — it **refuses to expose
itself beyond loopback** unless you pass `--allow-non-loopback`
explicitly. Default-safe stance: the agent is reachable only by the
local OS user on this machine. See
[`docs/OPERATIONS.md`](docs/OPERATIONS.md#bind-policy-loopback-by-default)
for opt-in setups (reverse proxy, k8s, internal corp net).

The bundled dashboard is at `http://127.0.0.1:7331/` and Prometheus
metrics at `/metrics`. **Open the dashboard URL in any browser** — when
auth is configured, the page detects the missing token and renders an
in-app login gate. Paste the value of `OXENCLAW_GATEWAY_TOKEN`, click
*Connect*, and the dashboard remembers it for 12 hours via cookie +
`localStorage` so reloads need nothing extra.

You can also bypass the form entirely with
`http://127.0.0.1:7331/?token=<OXENCLAW_GATEWAY_TOKEN>` — the gateway
sets the cookie on first response and the dashboard JS strips the
token out of the address bar so it doesn't leak via screenshots or
browser history. `/healthz`, `/readyz`, `/metrics` always remain
unauthenticated for orchestrator probes.

Open the dashboard or the desktop client and send a message — the
agent replies via the local model with full tool access.

### 4. Send a one-off message via CLI

```bash
oxenclaw message send --agent default "summarize today's news headlines"
```

---

## Clients

You can talk to the running gateway through three surfaces. They all
go through the same WS JSON-RPC endpoint and the same bearer-token
authentication.

### Browser dashboard (built-in, zero install)

The gateway ships a single-page dashboard at `http://localhost:7331/`
on the same port as the WS endpoint. Open it in any modern browser:

```
http://localhost:7331/
```

A login overlay appears the first time. Paste the token printed by
`oxenclaw gateway token` (or the value of `OXENCLAW_GATEWAY_TOKEN`).
Tick "Remember on this device" to write a 12-hour cookie + localStorage.

What you get:
- Chat tab with image upload (📎) for vision-capable models
- Sessions browser (list / preview / reset / fork / archive / delete)
- Cron, Approvals, Skills, Memory, Config, RPC log
- Light / dark theme toggle (top-right 🌓)
- Command palette (Ctrl+K)
- Responsive: < 900 px collapses the sidebar to a slide-in drawer

For deep walkthroughs of every interactive surface, see
[`tests/dashboard/README.md`](tests/dashboard/README.md) (the E2E
test catalogue is also a usage map).

### Native desktop app (Windows + Ubuntu)

Pre-built installers are attached to every GitHub Release at
[`/releases`](https://github.com/andreason21/oxenClaw/releases).
Pick the one for your OS:

| OS | File | Install |
|---|---|---|
| Windows 11 | `oxenclaw_X.Y.Z_x64_en-US.msi` | double-click, or `winget install oxenClaw.oxenClaw` |
| Windows 11 (no admin) | `oxenClaw_X.Y.Z_x64-setup.exe` | double-click (NSIS, per-user) |
| Ubuntu 22.04 | `oxenclaw_X.Y.Z_amd64_ubuntu22.04.deb` | `sudo apt install ./oxenclaw_*.deb` |
| Ubuntu 24.04 | `oxenclaw_X.Y.Z_amd64_ubuntu24.04.deb` | same with the matching file |
| Any glibc Linux | `oxenclaw_X.Y.Z_amd64_*.AppImage` | `chmod +x *.AppImage && ./oxenclaw_*.AppImage` |

First-run wizard asks for:
- **Gateway URL** — `http://localhost:7331` for a local agent, or the
  remote host's URL for a shared gateway.
- **Bearer token** — paste from `oxenclaw gateway token`. Stored in
  the OS keychain (Credential Manager on Windows, libsecret on Linux),
  never localStorage.
- (optional) **Auto-start on login**, **WSL auto-launch** (Windows only).

Updates are delivered automatically — the app polls a signed
`latest.json` on launch and applies new versions in the background
(MSI on Windows, AppImage on Linux). `.deb` installs are upgraded
through `apt` instead.

Full guide: [`docs/DESKTOP_APP.md`](docs/DESKTOP_APP.md).

### Frontier delegation via ACP

The local PiAgent model (Ollama / qwen3.5 / gemma4) is reliably weak
at long-horizon planning, multi-file refactors, and careful tool
sequencing. oxenClaw speaks the **Agent Client Protocol** (Zed's
`@agentclientprotocol/sdk` 0.19.x) over stdio so the model can
**delegate** those specific sub-tasks to a stronger external agent
mid-turn:

```python
delegate_to_acp(runtime="claude", prompt="…")    # registered by default
```

The handler spawns the runtime as a child stdio process, runs one
full ACP lifecycle (`initialize → session/new → session/prompt →
done`), collects the assistant text + a tool-call summary, and
returns. Failure modes (CLI not installed, timeout, wire error) all
surface as friendly tool-result strings — the parent turn never
crashes because of a delegation hop. Three runtimes are pre-mapped
(`claude` / `codex` / `gemini` → argv `[<name>, "acp"]`); pass
`runtime="custom"` with explicit `argv` for any other ACP server.

The reverse direction is also supported as a secondary capability —
external clients can drive our local PiAgent over stdio:

```bash
oxenclaw acp --backend pi
```

Connect Zed via `~/.config/zed/agent_servers.json`:

```json
{
  "oxenclaw": {
    "command": "oxenclaw",
    "args": ["acp", "--backend", "pi"]
  }
}
```

Full reference + Suwon-weather scenario walkthrough + the four-verb
lifecycle diagram: [`docs/ACP.md`](docs/ACP.md).

### Outbound channels (Slack)

The dashboard and desktop client are the bidirectional chat surfaces.
Slack is outbound-only — for cron alerts, agent-initiated pings, or
any other notification the agent needs to push into a workspace
channel. Configure in `~/.oxenclaw/config.yaml`:

```yaml
channels:
  slack:
    accounts:
      - account_id: alerts       # outbound-only — for #alerts notifications
```

Drop the bot token at `~/.oxenclaw/credentials/slack/<account_id>.json`
(mode 0600). After `oxenclaw gateway start` picks it up:

- **Slack** — push notifications via `chat.postMessage`. Walk-through
  for Enterprise Grid + corp proxies in [`docs/SLACK.md`](docs/SLACK.md).
- **Custom channel** — ship a Python plugin with a
  `oxenclaw.plugins` entry point; the runner picks it up at gateway
  boot. See "Add a custom channel" below.

---

## Architecture

```
   ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
   │ Tauri desktop    │  │ Browser dashboard│  │ Outbound         │
   │ (Win .msi /      │  │ (built-in SPA at │  │ - Slack          │
   │  Ubuntu .deb /   │  │  port 7331)      │  │ - …pluggable     │
   │  .AppImage)      │  │                  │  │                  │
   └────────┬─────────┘  └─────────┬────────┘  └────────┬─────────┘
            │ WS+token, Origin-locked│                   │
            └───────────────┬────────┴───────────────────┘
                            │
                   ┌────────┴───────────────────────────────┐
                   │           GATEWAY (port 7331)          │
                   │  WS JSON-RPC + HTTP /metrics /healthz  │
                   │  /readyz / dashboard / static assets   │
                   └────────────────┬───────────────────────┘
                                    │
   ┌────────────────────────────────┴────────────────────────────┐
   │ Agent runtime                                               │
   │  - LocalAgent  (Ollama / vLLM / any OpenAI-compatible HTTP) │
   │  - PiAgent     (22 hosted providers, incl. Anthropic / GPT  │
   │                 / Gemini / Bedrock / Groq …)                │
   │  - EchoAgent   (test fixture)                               │
   └───────────────────────────┬─────────────────────────────────┘
                               │
   ┌───────────────────────────┴──────────────────────────────┐
   │  Tools · Skills · MCP client · Memory · Wiki             │
   │  Approvals · Cron · NetPolicy · Sandbox                  │
   └──────────────────────────────────────────────────────────┘
```

| Layer | What lives there |
|---|---|
| `gateway/` | WS JSON-RPC server, HTTP routes (`/metrics`, `/healthz`, `/readyz`, dashboard), per-connection concurrency cap, bearer auth, graceful shutdown |
| `agents/` | Agent registry, factory, `LocalAgent` (Ollama / vLLM / OpenAI-compatible), `PiAgent` (22 providers via `pi/`), `EchoAgent` |
| `channels/` | Channel abstraction, router, runner supervisor (restart-on-error with backoff) |
| `extensions/slack/` | First-party Slack outbound plugin (Web API `chat.postMessage`) |
| `extensions/dashboard/` | Built-in dashboard / desktop-client channel — agent replies surface via `chat.history` |
| `pi/` | The pi-embedded-runner port — provider wrappers, run loop, compaction, persistence, system-prompt assembly, cache observability, tool runtime, MCP client |
| `memory/` | sqlite-vec vector store + FTS5 + MMR + embedding cache |
| `wiki/` | "Memory wiki" — durable, claim-tracked knowledge base |
| `tools_pkg/` | Bundled tools: web fetch/search, subagent, cron, message, coding agent, summarize, weather, github, healthcheck, skill creator, session logs |
| `clawhub/` | Skill installer + loader + frontmatter parser, sourced from package registries |
| `approvals/` | Human-in-the-loop approval manager, `gated_tool` wrapper |
| `cron/` | APScheduler-backed cron with WAL-persisted jobs and timezone safety |
| `security/net/` | `NetPolicy`, SSRF guard, DNS pinning, outbound audit store, webhook HMAC + rate limiting |
| `observability/` | Metrics registry, readiness checker, structured JSON logging |
| `backup/` | Online SQLite backup + tar.gz archive with per-file SHA256 manifest |
| `config/` | YAML loader, env var substitution, paths, preflight validator |

Detailed map: [`docs/SUBSYSTEM_MAP.md`](docs/SUBSYSTEM_MAP.md). Full
porting plan: [`docs/PORTING_PLAN.md`](docs/PORTING_PLAN.md).

---

## What you can do with it

### Build your own tool / skill (recommended)

```python
# ~/.oxenclaw/skills/ticket-lookup/ticket_lookup.py
from pydantic import BaseModel, Field
from oxenclaw.agents.tools import FunctionTool

class _Args(BaseModel):
    ticket_id: str = Field(..., description="Linear ticket id")

def ticket_lookup_tool():
    async def _h(args: _Args) -> str:
        return f"# {args.ticket_id}\n\n(body)"
    return FunctionTool(
        name="ticket_lookup",
        description="Fetch a Linear ticket and return title + body.",
        input_model=_Args,
        handler=_h,
    )
```

Plus a `SKILL.md` next to it telling the model when to use the tool.
Full guide: [`docs/AUTHORING_SKILLS.md`](docs/AUTHORING_SKILLS.md).

### Import any existing MCP server

`~/.oxenclaw/mcp.json` (same shape Claude Desktop / mcp-cli use):

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    },
    "yfinance": {
      "command": "/home/you/yfmcp-venv/bin/yfmcp",
      "args": [],
      "transport": "stdio"
    }
  }
}
```

The server's tools become first-class tools the agent can call. For a
verified end-to-end walkthrough (Yahoo Finance via `yfmcp`, including
configure → connect → direct call → agent wiring), see
[`docs/MCP_YAHOO_FINANCE.md`](docs/MCP_YAHOO_FINANCE.md).

### Schedule recurring work

```bash
oxenclaw message send --agent default "summarize my Slack DMs hourly"
# ... agent invokes the cron tool to register the job
```

### Add a custom channel

Drop a Python package with a `oxenclaw.plugins` entry point:

```toml
# pyproject.toml of your plugin
[project.entry-points."oxenclaw.plugins"]
discord = "my_pkg.discord_plugin:DISCORD_PLUGIN"
```

`pip install -e .` and the plugin loads on next gateway restart.

---

## Operations

Production deployment guide: [`docs/OPERATIONS.md`](docs/OPERATIONS.md).
Highlights:

- **systemd unit** with `SIGTERM`-driven graceful shutdown.
- **Prometheus alerts** on RPC error rate, p99 turn duration,
  approval backlog.
- **`oxenclaw backup create/verify/restore`** with consistent SQLite
  snapshots even while the gateway runs.
- **`scripts/soak.py --duration 14400`** for pre-release stability
  validation (CSV trace + automatic memory/FD-leak threshold check).
- **`OXENCLAW_LOG_FORMAT=json`** for log aggregators (Loki/Datadog/…).
  Every log line carries the `trace_id` of the originating RPC.

---

## Status

| Component | Status |
|---|---|
| Core gateway, agent runtime, dashboard + desktop chat surface | ✅ Production-ready |
| Memory + sessions + wiki | ✅ |
| MCP **client** (import existing servers) | ✅ |
| Browser tools (BR-1, fail-closed Playwright) | ✅ Opt-in via `OXENCLAW_ENABLE_BROWSER=1` |
| Canvas tools (CV-1, dashboard-embedded HTML) | ✅ Opt-in via `OXENCLAW_ENABLE_CANVAS=1` |
| MCP **server** (expose oxenClaw to other clients) | ⏳ Future phase |
| Slack (outbound notifications) | ✅ Enterprise-Grid-friendly, alert-only — see [`docs/SLACK.md`](docs/SLACK.md) |
| Discord + 4 more channels (full bidirectional) | ⏳ Future phase |
| Native mobile / desktop apps | ❌ Out of scope |
| Full React web UI | ❌ Bundled single-page dashboard only |

**Test suite: 1026 pass / 33 skip** unit (10 environment-gated + 23
dashboard E2E that auto-skip when Chromium system libs are missing —
see [`tests/dashboard/README.md`](tests/dashboard/README.md)) + **9
pass** live `gemma4:latest` integration. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full module
map, [`docs/BROWSER.md`](docs/BROWSER.md) for the browser tool surface,
[`docs/CANVAS.md`](docs/CANVAS.md) for the canvas panel, and
[`docs/MCP_YAHOO_FINANCE.md`](docs/MCP_YAHOO_FINANCE.md) for a worked
MCP-client integration.

---

## Documentation

| Document | Purpose |
|---|---|
| [`docs/ACP.md`](docs/ACP.md) | Agent Client Protocol — `oxenclaw acp` agent + `SubprocessAcpRuntime` client + worked Suwon-weather scenario |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Reference architecture extracted from openclaw |
| [`docs/INSTALL_WSL.md`](docs/INSTALL_WSL.md) | Windows install via WSL2 (English + Korean) |
| [`docs/PORTING_PLAN.md`](docs/PORTING_PLAN.md) | Phased roadmap (D → B → A → M → PROD) |
| [`docs/SUBSYSTEM_MAP.md`](docs/SUBSYSTEM_MAP.md) | What's ported / partial / out-of-scope |
| [`docs/AUTHORING_SKILLS.md`](docs/AUTHORING_SKILLS.md) | Build your own tools and skills |
| [`docs/BROWSER.md`](docs/BROWSER.md) | Headless-Chromium tool surface (BR-1) — fail-closed egress |
| [`docs/CANVAS.md`](docs/CANVAS.md) | Dashboard-embedded canvas (CV-1) — sandboxed iframe |
| [`docs/SECURITY.md`](docs/SECURITY.md) | Threat model + layered defenses |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | Install, run, observe, backup, recover |
| [`docs/CONFIG_EXAMPLE.yaml`](docs/CONFIG_EXAMPLE.yaml) | Annotated config sample |
| [`docs/MEMORY_COMPARISON.md`](docs/MEMORY_COMPARISON.md) | Memory subsystem vs openclaw |

---

## License

MIT.

---

## 한국어

자체 호스팅 가능한 사내용 AI 어시스턴트 게이트웨이. Python 구현.
사용자 자신의 모델(로컬 Ollama, Anthropic, 또는 `pi` 런너로 22개
프로바이더)을 번들 웹 대시보드 또는 네이티브 데스크톱 앱(Windows /
Ubuntu)으로 사용하고, 도구·스킬을 부여한 뒤 특별한 알림은 Slack
아웃바운드로 흘려보낸다 — 모두 프로덕션급 관측성을 갖춘 장기 실행
서비스 위에서.

> [openclaw](https://github.com/openclaw/openclaw)의 Python 포트 —
> 서버/CLI 부분만 추려서 자체 호스팅이 가능하도록 강화·문서화한 버전.

### 왜 oxenClaw?

| | |
|---|---|
| 🦙 **모델 자유** | 기본은 로컬 Ollama (도구 호출 가능한 모델 아무거나). Anthropic, OpenAI 호환, Bedrock, Google, Groq, DeepSeek, Mistral, Together, Fireworks 등 `pi` 통해 22개 프로바이더 지원. |
| 🖼️ **멀티모달 기본 지원** | 대시보드 chat 또는 데스크톱 앱에서 📎 버튼으로 사진을 첨부하면 vision 가능 모델(gemma4 / Claude 3+ / GPT-4o / Gemini 1.5+ / llava 등)이 그 자리에서 본다. Vision 미지원 모델은 텍스트 컨텍스트에 "이미지 N장 드롭됨" 안내가 자동으로 들어간다. |
| 🖥️ **번들 대시보드 SPA** | 라이트/다크 테마 토글, Ctrl+K command palette, 세션 브라우저(리스트/미리보기/리셋/포크/아카이브), 모바일 반응형 drawer, in-app 로그인 게이트. 빌드 단계 없음, JSON-RPC 웹소켓과 동일 포트에서 서빙. |
| 💻 **네이티브 데스크톱 앱 (Windows + Ubuntu)** | Tauri 기반 클라이언트 — Windows 11 (`.msi` / NSIS `.exe`), Ubuntu 22.04 + 24.04 (`.deb`), 범용 Linux (`.AppImage`). OS 키체인 토큰 저장, 네이티브 토스트 알림, 시스템 트레이, Origin 제한 WS upgrade, Ed25519 서명 자동 업데이트. [`docs/DESKTOP_APP.md`](docs/DESKTOP_APP.md) 참고. |
| 🔌 **개방형 설계** | Plugin SDK + entry-point 자동 디스커버리. 새 채널·스킬은 `pip install`로 끝. |
| 🛡️ **프로덕션급 보안** | NetPolicy + DNS pinning + SSRF 가드, 도구 격리 실행 (RLIMIT + bwrap), 사람 승인 게이트, 서브프로세스 MCP 서버용 위험 env 스트립. |
| 📊 **운영을 진지하게** | Prometheus `/metrics`, `/healthz` + `/readyz`, RPC 단위 `trace_id` 가 박힌 구조화 JSON 로그, SIGTERM 그레이스풀 드레인, 온라인 SQLite 백업/복구. |
| 🧠 **내장 메모리** | sqlite-vec 벡터 스토어 + FTS5 + MMR 재정렬 + 임베딩 캐시. WAL 영속 세션, 자동 압축, 영속 지식 베이스 ("memory wiki"). |
| 🛠️ **확장 쉬움** | `SKILL.md` + Python 도구 두 파일이면 끝. 또는 `mcp.json`에 기존 MCP 서버 등록. |
| 🪞 **클라우드 종속 없음** | 노트북, Raspberry Pi, systemd 어디서든. 모든 상태는 `~/.oxenclaw/` 아래. |

### 설치

> **Windows 사용자?** WSL2 전용 가이드 참고:
> [`docs/INSTALL_WSL.md`](docs/INSTALL_WSL.md). Win32 네이티브는 미지원
> (샌드박스 + 시그널 + Linux 네트워킹 의존).

```bash
git clone https://github.com/andreason21/oxenClaw.git
cd oxenClaw
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

oxenclaw paths
oxenclaw config validate
```

**Linux / macOS / WSL2** 지원. Python **3.11+** 필요. 기본 LLM 백엔드는
[Ollama](https://ollama.ai/)
(`127.0.0.1:11434`). Ollama 설치 후 도구 호출 가능한 모델을 받는다, 예:

```bash
ollama pull qwen3.5:9b
```

기본값은 `--model <id>`로 오버라이드. 검증된 모델:

| 모델 | 컨텍스트 | 비고 |
|---|---|---|
| **`qwen3.5:9b`** *(기본값)* | 256K | 멀티모달(vision), 네이티브 함수 호출 + thinking, 약 6.6 GB (Q4_K_M). 라이브 e2e 18/18 PASS. |
| `gemma4:latest` (= `e4b`) | 128K | 멀티모달(text+image), 네이티브 함수 호출, 약 9.6 GB. |
| `gemma4:e2b` | 128K | 더 가벼움 (약 7.2 GB) — 같은 계열의 작은 변종. |
| `gemma4:26b` / `31b` | 256K | 고RAM 환경용 MoE 변종. |
| `qwen2.5:7b-instruct` | 32K | 강한 도구 호출. |
| `llama3.1:8b` | 128K | 범용성 좋음. |
| `mistral-nemo:12b` | 128K | 느리지만 verbose. |

LLM 없이 RPC + 도구만 테스트하려면 `--provider echo` 사용.

#### 내부 vLLM 서버 사용

팀 내부에 `vllm serve …`로 띄운 vLLM 박스가 있다면 Ollama 없이 바로 그쪽으로
붙일 수 있다:

```bash
oxenclaw gateway start \
    --provider vllm \
    --base-url http://internal-vllm.lan:8000/v1 \
    --model meta-llama/Llama-3.1-8B-Instruct \
    --api-key "$VLLM_API_KEY"   # vLLM을 --api-key로 띄운 경우만
```

`--provider vllm`은 `local`의 strict-OpenAI 변종 — Ollama 전용 필드
(`num_predict`)나 워머핑 핑을 보내지 않는다. `--base-url` 기본값은
`http://127.0.0.1:8000/v1`. `--api-key`는 선택 — vLLM을 `--api-key`로
시작한 경우에만 필요. `config.yaml`도 같은 형태로 동작한다:

```yaml
agents:
  default:
    provider: vllm
    model: meta-llama/Llama-3.1-8B-Instruct
    base_url: http://internal-vllm.lan:8000/v1
    api_key: ${VLLM_API_KEY}
```

### 빠른 시작

#### 1. 최소 설정

`~/.oxenclaw/config.yaml`:

```yaml
channels: {}
agents:
  default:
    id: default
    provider: local
    model: qwen3.5:9b
    system_prompt: |
      You are a helpful assistant.
```

#### 2. (선택) Slack 아웃바운드 연결

특별한 경우(크론 알림, 에이전트 발신 푸시 등)에만 Slack으로 아웃바운드.
워크스페이스 봇 토큰을 `~/.oxenclaw/credentials/slack/main.json`에
저장:

```json
{ "token": "xoxb-..." }
```

`config.yaml`에 바인딩 추가:

```yaml
channels:
  slack:
    accounts:
      - account_id: main
        display_name: "Workspace Alerts"
```

#### 3. 게이트웨이 실행

```bash
export OXENCLAW_GATEWAY_TOKEN=$(openssl rand -hex 32)
oxenclaw gateway start --provider local
```

게이트웨이는 `127.0.0.1`만 바인딩한다 — `--allow-non-loopback`을
명시적으로 주지 않으면 **루프백 외부 노출은 거부**된다. 기본 보안 스탠스:
에이전트는 이 머신의 로컬 OS 사용자만 접근 가능. 리버스 프록시, k8s,
사내망 등 opt-in 셋업은
[`docs/OPERATIONS.md`](docs/OPERATIONS.md#bind-policy-loopback-by-default)
참고.

번들 대시보드: `http://127.0.0.1:7331/`. Prometheus: `/metrics`.
**브라우저에서 대시보드 URL을 그냥 열기** — 인증이 설정된 상태면 SPA가
토큰 없음을 감지하고 화면 안에 로그인 폼을 띄움. `OXENCLAW_GATEWAY_TOKEN`
값을 붙여넣고 *Connect* — 12시간 쿠키 + `localStorage`에 저장되어 새로고침
시 별도 입력 불필요.

URL 한 방으로 끝내고 싶으면 `http://127.0.0.1:7331/?token=<OXENCLAW_GATEWAY_TOKEN>`
도 가능 — 게이트웨이가 응답에 쿠키를 설정하고 SPA가 주소창에서 토큰을
제거 (스크린샷·브라우저 히스토리 누출 방지). `/healthz`, `/readyz`,
`/metrics`는 오케스트레이터 프로브용으로 항상 비인증 유지.

대시보드 또는 데스크톱 앱에서 메시지를 보내면 로컬 모델이 도구를 사용해 답한다.

#### 4. CLI에서 일회성 메시지

```bash
oxenclaw message send --agent default "오늘 뉴스 헤드라인 요약해줘"
```

### 클라이언트

게이트웨이에는 3가지 클라이언트 경로가 있다. 모두 동일한 WS JSON-RPC
엔드포인트와 동일한 Bearer 토큰 인증을 거친다.

#### 브라우저 대시보드 (번들, 설치 불필요)

게이트웨이가 시작되면 WS 엔드포인트와 같은 포트에서 SPA를 같이 서빙한다.
브라우저로 열기만 하면 끝:

```
http://localhost:7331/
```

처음 열면 로그인 오버레이가 뜬다. `oxenclaw gateway token`이 출력한
토큰 (또는 `OXENCLAW_GATEWAY_TOKEN` 값)을 붙여넣고 "Remember on this
device"를 체크하면 12시간 쿠키 + localStorage 에 저장된다.

제공 기능:
- Chat 탭 + 이미지 첨부 (📎) — vision 가능 모델 자동 인식
- 세션 브라우저 (리스트 / 미리보기 / 리셋 / 포크 / 아카이브 / 삭제)
- Cron, Approvals, Skills, Memory, Config, RPC log
- 라이트/다크 테마 토글 (우상단 🌓)
- Command palette (Ctrl+K)
- 반응형: 900 px 미만에서 사이드바가 슬라이드 drawer로 전환

세부 동작은 [`tests/dashboard/README.md`](tests/dashboard/README.md)
의 E2E 테스트 카탈로그 참고 (사용 가이드 역할도 함).

#### 네이티브 데스크톱 앱 (Windows + Ubuntu)

매 GitHub Release에 사전 빌드된 인스톨러가 첨부된다 →
[`/releases`](https://github.com/andreason21/oxenClaw/releases).
OS에 맞는 파일을 받는다:

| OS | 파일 | 설치 |
|---|---|---|
| Windows 11 | `oxenclaw_X.Y.Z_x64_en-US.msi` | 더블클릭, 또는 `winget install oxenClaw.oxenClaw` |
| Windows 11 (관리자 없음) | `oxenClaw_X.Y.Z_x64-setup.exe` | 더블클릭 (NSIS, per-user) |
| Ubuntu 22.04 | `oxenclaw_X.Y.Z_amd64_ubuntu22.04.deb` | `sudo apt install ./oxenclaw_*.deb` |
| Ubuntu 24.04 | `oxenclaw_X.Y.Z_amd64_ubuntu24.04.deb` | 동일 (24.04용 파일로) |
| 기타 glibc Linux | `oxenclaw_X.Y.Z_amd64_*.AppImage` | `chmod +x *.AppImage && ./oxenclaw_*.AppImage` |

첫 실행 마법사가 묻는 것:
- **Gateway URL** — 로컬 에이전트면 `http://localhost:7331`, 원격이면
  해당 호스트의 URL.
- **Bearer 토큰** — `oxenclaw gateway token`에서 복사. OS 키체인
  (Windows = Credential Manager, Linux = libsecret) 에 저장되며
  localStorage 에는 절대 안 들어간다.
- (선택) **로그인 시 자동 시작**, **WSL 자동 부팅** (Windows 전용).

자동 업데이트가 내장되어 있다 — 부팅 시 서명된 `latest.json`을 폴링해서
새 버전을 백그라운드로 적용 (Windows MSI / Linux AppImage). `.deb`
설치본은 `apt` 로 직접 업그레이드.

전체 가이드: [`docs/DESKTOP_APP.md`](docs/DESKTOP_APP.md).

#### Frontier 위임 (ACP)

로컬 PiAgent 모델(Ollama / qwen3.5 / gemma4)은 장기 계획, 다중 파일
리팩토링, 정교한 툴 시퀀싱에 약하다. oxenClaw가 **Agent Client
Protocol** (Zed의 `@agentclientprotocol/sdk` 0.19.x) 을 stdio로
지원하는 *주된 이유*는 모델이 그런 어려운 sub-task를 한 턴 단위로
**더 강한 외부 에이전트에 위임**할 수 있게 하기 위함:

```python
delegate_to_acp(runtime="claude", prompt="…")    # 기본 번들에 등록
```

핸들러가 해당 런타임을 자식 stdio 프로세스로 띄우고 ACP 한 사이클
(`initialize → session/new → session/prompt → done`)을 통과시켜
어시스턴트 텍스트 + tool-call 요약을 수집해서 반환. 실패 모드(CLI
없음, timeout, wire error)는 모두 친화적 문자열로 surface — 위임
홉 때문에 부모 턴이 깨지지 않는다. 세 런타임이 사전 매핑됨
(`claude` / `codex` / `gemini` → argv `[<name>, "acp"]`); 다른
ACP 서버는 `runtime="custom"` + 명시적 `argv`.

역방향도 부수적 기능으로 지원 — 외부 클라이언트가 우리 로컬
PiAgent를 stdio로 구동할 수 있다:

```bash
oxenclaw acp --backend pi
```

`~/.config/zed/agent_servers.json`로 Zed 연결:

```json
{
  "oxenclaw": {
    "command": "oxenclaw",
    "args": ["acp", "--backend", "pi"]
  }
}
```

전체 레퍼런스 + 수원 날씨 시나리오 + 4-verb 라이프사이클:
[`docs/ACP.md`](docs/ACP.md).

#### 아웃바운드 채널 (Slack)

대시보드 / 데스크톱 앱이 양방향 chat surface. Slack은 아웃바운드
전용 — cron 알림이나 에이전트 발신 푸시 같은 특별한 경우에만 사용.
`~/.oxenclaw/config.yaml`에서 설정:

```yaml
channels:
  slack:
    accounts:
      - account_id: alerts       # 아웃바운드 전용 — #alerts 알림용
```

봇 토큰은 `~/.oxenclaw/credentials/slack/<account_id>.json`
(권한 0600) 에 저장. 게이트웨이 재시작 시 자동 로드:

- **Slack** — `chat.postMessage` 통한 알림 발송. Enterprise Grid +
  사내 프록시 셋업은 [`docs/SLACK.md`](docs/SLACK.md).
- **커스텀 채널** — `oxenclaw.plugins` entry point 가진 Python 패키지
  배포하면 게이트웨이 부팅 시 자동 로드. 아래 "커스텀 채널 추가" 참고.

### 아키텍처

```
   ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
   │ Tauri 데스크톱앱 │  │ 브라우저 대시보드│  │ 아웃바운드        │
   │ (Win .msi /      │  │ (번들 SPA,       │  │ - Slack           │
   │  Ubuntu .deb /   │  │  port 7331)      │  │ - …플러그인 가능  │
   │  .AppImage)      │  │                  │  │                   │
   └────────┬─────────┘  └─────────┬────────┘  └────────┬─────────┘
            │ WS+토큰, Origin 잠금│                     │
            └───────────────┬─────┴─────────────────────┘
                            │
                   ┌────────┴────────────────────────────────┐
                   │           GATEWAY (port 7331)           │
                   │  WS JSON-RPC + HTTP /metrics /healthz   │
                   │  /readyz / dashboard / 정적 자원        │
                   └────────────────┬────────────────────────┘
                                    │
   ┌────────────────────────────────┴──────────────────────────────┐
   │ 에이전트 런타임                                               │
   │  - LocalAgent  (Ollama / vLLM / OpenAI 호환 HTTP)             │
   │  - PiAgent     (22개 호스티드 프로바이더 — Anthropic / GPT /  │
   │                 Gemini / Bedrock / Groq …)                    │
   │  - EchoAgent   (테스트용)                                     │
   └───────────────────────────┬───────────────────────────────────┘
                               │
   ┌───────────────────────────┴───────────────────────────────┐
   │  도구 · 스킬 · MCP 클라이언트 · 메모리 · 위키              │
   │  승인 · cron · NetPolicy · 샌드박스                       │
   └────────────────────────────────────────────────────────────┘
```

| 계층 | 하는 일 |
|---|---|
| `gateway/` | WS JSON-RPC 서버, HTTP 라우트, 연결당 동시성 한도, Bearer 인증, 그레이스풀 종료 |
| `agents/` | 에이전트 레지스트리·팩토리, LocalAgent / PiAgent / EchoAgent |
| `channels/` | 채널 추상화, 라우터, 슈퍼바이저(에러 시 백오프 재시작) |
| `extensions/slack/` | 1st-party Slack 아웃바운드 플러그인 (Web API `chat.postMessage`) |
| `extensions/dashboard/` | 번들 dashboard / 데스크톱 클라이언트 채널 — 에이전트 답변은 `chat.history`로 surface |
| `pi/` | pi-embedded-runner 포트 — 프로바이더 래퍼, 런 루프, 압축, 영속, 시스템 프롬프트 조립, 캐시 옵저버, 도구 런타임, MCP 클라이언트 |
| `memory/` | sqlite-vec + FTS5 + MMR + 임베딩 캐시 |
| `wiki/` | "memory wiki" — 영속 지식 베이스 |
| `tools_pkg/` | 번들 도구 (web fetch/search, subagent, cron, message, coding agent, …) |
| `clawhub/` | 스킬 인스톨러·로더·frontmatter 파서 |
| `approvals/` | 사람 승인 매니저, `gated_tool` 래퍼 |
| `cron/` | APScheduler 기반, WAL 영속, 타임존 안전 |
| `security/net/` | NetPolicy, SSRF 가드, DNS pinning, 아웃바운드 감사, 웹훅 HMAC + 레이트리밋 |
| `observability/` | 메트릭 레지스트리, 레디니스 체커, 구조화 JSON 로깅 |
| `backup/` | 온라인 SQLite 백업 + tar.gz 아카이브, 파일별 SHA256 매니페스트 |
| `config/` | YAML 로더, env 치환, 경로, preflight 검증 |

상세 맵은 [`docs/SUBSYSTEM_MAP.md`](docs/SUBSYSTEM_MAP.md), 풀
포팅 계획은 [`docs/PORTING_PLAN.md`](docs/PORTING_PLAN.md).

### 사용자가 할 수 있는 것

#### 자기 도구·스킬 만들기 (권장)

```python
# ~/.oxenclaw/skills/ticket-lookup/ticket_lookup.py
from pydantic import BaseModel, Field
from oxenclaw.agents.tools import FunctionTool

class _Args(BaseModel):
    ticket_id: str = Field(..., description="Linear 티켓 id")

def ticket_lookup_tool():
    async def _h(args: _Args) -> str:
        return f"# {args.ticket_id}\n\n(본문)"
    return FunctionTool(
        name="ticket_lookup",
        description="Linear 티켓을 가져와 제목 + 본문을 마크다운으로 반환.",
        input_model=_Args,
        handler=_h,
    )
```

같은 폴더에 `SKILL.md`로 사용 시점을 모델에게 설명. 풀 가이드:
[`docs/AUTHORING_SKILLS.md`](docs/AUTHORING_SKILLS.md).

#### 기존 MCP 서버 가져오기

`~/.oxenclaw/mcp.json` (Claude Desktop / mcp-cli와 동일 스키마):

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    },
    "yfinance": {
      "command": "/home/you/yfmcp-venv/bin/yfmcp",
      "args": [],
      "transport": "stdio"
    }
  }
}
```

서버의 도구가 oxenClaw 네이티브 도구처럼 노출된다. 전체 시나리오
(설치 → 연결 → 직접 호출 → 에이전트 연결까지)를 검증한 예제는
[`docs/MCP_YAHOO_FINANCE.md`](docs/MCP_YAHOO_FINANCE.md) 참고.

#### 반복 작업 스케줄링

```bash
oxenclaw message send --agent default "매시간 Slack DM 요약해줘"
# 에이전트가 cron 도구를 호출해서 작업 등록
```

#### 커스텀 채널 추가

`oxenclaw.plugins` entry-point가 있는 Python 패키지 작성:

```toml
[project.entry-points."oxenclaw.plugins"]
discord = "my_pkg.discord_plugin:DISCORD_PLUGIN"
```

`pip install -e .` 후 게이트웨이 재시작이면 끝.

### 운영

프로덕션 배포 가이드: [`docs/OPERATIONS.md`](docs/OPERATIONS.md). 요점:

- **systemd 유닛** + SIGTERM 그레이스풀 종료
- **Prometheus 알람** RPC 에러율, p99 턴 지연, 승인 백로그 등
- **`oxenclaw backup create/verify/restore`** — 게이트웨이 가동 중에도
  일관된 SQLite 스냅샷
- **`scripts/soak.py --duration 14400`** 릴리스 전 안정성 검증
  (CSV trace + 메모리/FD 누수 임계 자동 검사)
- **`OXENCLAW_LOG_FORMAT=json`** 로그 집약기용. 모든 라인에
  발신 RPC의 `trace_id`가 박힌다.

### 상태

| 구성 요소 | 상태 |
|---|---|
| 코어 게이트웨이, 에이전트 런타임, 대시보드 + 데스크톱 chat surface | ✅ 프로덕션 가능 |
| 메모리 + 세션 + 위키 | ✅ |
| MCP **클라이언트** (외부 서버 흡수) | ✅ |
| 브라우저 도구 (BR-1, fail-closed Playwright) | ✅ `OXENCLAW_ENABLE_BROWSER=1` 옵트인 |
| 캔버스 도구 (CV-1, dashboard 임베드 HTML) | ✅ `OXENCLAW_ENABLE_CANVAS=1` 옵트인 |
| MCP **서버** (oxenClaw를 외부 클라이언트에 노출) | ⏳ 차후 |
| Slack (아웃바운드 알림) | ✅ Enterprise Grid 호환, 알림 전용 — [`docs/SLACK.md`](docs/SLACK.md) |
| Discord + 추가 채널 4종 (양방향) | ⏳ 차후 |
| 네이티브 모바일·데스크톱 앱 | ❌ 범위 외 |
| 풀 React 웹 UI | ❌ 번들 단일 페이지 대시보드만 |

**테스트: 1026 pass / 33 skip** (단위 — 환경 의존 10 + 대시보드 E2E 23개는
Chromium 시스템 라이브러리 없을 때 자동 skip, 자세한 건
[`tests/dashboard/README.md`](tests/dashboard/README.md) 참고) + **9 pass**
(live `gemma4:latest` 통합).

### 문서

| 문서 | 용도 |
|---|---|
| [`docs/ACP.md`](docs/ACP.md) | Agent Client Protocol — `oxenclaw acp` 에이전트 + `SubprocessAcpRuntime` 클라이언트 + 수원 날씨 시나리오 |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | openclaw에서 추출한 레퍼런스 아키텍처 |
| [`docs/INSTALL_WSL.md`](docs/INSTALL_WSL.md) | Windows WSL2 설치 가이드 (영문 + 한글) |
| [`docs/PORTING_PLAN.md`](docs/PORTING_PLAN.md) | 단계별 로드맵 (D → B → A → M → PROD) |
| [`docs/SUBSYSTEM_MAP.md`](docs/SUBSYSTEM_MAP.md) | 포팅 / 부분 / 범위외 분류 |
| [`docs/AUTHORING_SKILLS.md`](docs/AUTHORING_SKILLS.md) | 자기 도구·스킬 작성 가이드 |
| [`docs/BROWSER.md`](docs/BROWSER.md) | 헤드리스 Chromium 도구 (BR-1) — egress fail-closed |
| [`docs/CANVAS.md`](docs/CANVAS.md) | dashboard 임베드 캔버스 (CV-1) — sandboxed iframe |
| [`docs/SECURITY.md`](docs/SECURITY.md) | 위협 모델 + 다층 방어 |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | 설치 · 실행 · 관측 · 백업 · 복구 |
| [`docs/CONFIG_EXAMPLE.yaml`](docs/CONFIG_EXAMPLE.yaml) | 주석 달린 설정 샘플 |
| [`docs/MEMORY_COMPARISON.md`](docs/MEMORY_COMPARISON.md) | 메모리 서브시스템 vs openclaw |

### 라이선스

MIT.
