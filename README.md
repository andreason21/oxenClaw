# sampyClaw

[![ci](https://github.com/andreason21/sampyClaw/actions/workflows/ci.yml/badge.svg)](https://github.com/andreason21/sampyClaw/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A self-hosted, multi-channel AI assistant gateway in Python. Bring your
own model (local Ollama, Anthropic, or 22 providers via the bundled `pi`
runner), wire it to messengers like Telegram, give it tools and skills,
and run it as a long-lived service with production-grade observability.

> Python port of [openclaw](https://github.com/openclaw/openclaw) — the
> server/CLI surface, hardened and documented for self-hosting.

[**한국어 README ↓**](#한국어)

---

## Why sampyClaw?

| | |
|---|---|
| 🦙 **Bring your own model** | Local Ollama by default (any tool-capable model). Anthropic, OpenAI-compatible, Bedrock, Google, Groq, DeepSeek, Mistral, Together, Fireworks, … 22 providers via `pi`. |
| 🖼️ **Multimodal in/out of the box** | Send a photo through Telegram **or attach one in the dashboard chat** (📎 button) and a vision-capable model (gemma4 / Claude 3+ / GPT-4o / Gemini 1.5+ / llava / etc.) sees it. Models without vision get a dropped-image notice in their text context. |
| 🖥️ **Bundled dashboard SPA** | Light/dark theme toggle, Ctrl+K command palette, sessions browser (list/preview/reset/fork/archive), responsive mobile drawer, in-app login gate. No build step, served on the same port as the JSON-RPC websocket. |
| 💻 **Native desktop app (Windows + Ubuntu)** | Tauri client for Windows 11 (`.msi` / `.exe`), Ubuntu 22.04 + 24.04 (`.deb`), or any glibc Linux (`.AppImage`). OS keychain–backed tokens, native toast notifications, system tray, Origin-locked WS upgrade, Ed25519-signed auto-updates. See [`docs/DESKTOP_APP.md`](docs/DESKTOP_APP.md). |
| 🔌 **Open by design** | Plugin SDK + entry-point discovery. New channels and skills install with `pip install`. |
| 🛡️ **Production-grade security** | NetPolicy + DNS pinning + SSRF guards, sandboxed tool execution (RLIMIT + bwrap), human-in-the-loop approval gating, dangerous-env stripping for subprocess MCP servers. |
| 📊 **Operationally serious** | Prometheus `/metrics`, `/healthz` + `/readyz`, structured JSON logs with per-RPC `trace_id`, graceful SIGTERM drain, online SQLite backup/restore. |
| 🧠 **Memory built-in** | sqlite-vec vector store + FTS5 + MMR rerank + embedding cache. Sessions persist with WAL; auto-compaction; durable knowledge base ("memory wiki"). |
| 🛠️ **Easy to extend** | Two files (`SKILL.md` + a Python tool) and your tool is in. Or import any existing MCP server via `mcp.json`. |
| 🪞 **No cloud lock-in** | Runs on a laptop, Pi, or systemd unit. All state under `~/.sampyclaw/`. |

---

## Install

> **On Windows?** Use the dedicated WSL2 guide:
> [`docs/INSTALL_WSL.md`](docs/INSTALL_WSL.md). Native Win32 is not
> supported (sandbox + signal + Linux networking dependencies).

```bash
# Clone and install in editable mode
git clone https://github.com/andreason21/sampyClaw.git
cd sampyClaw
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Sanity check
sampyclaw paths
sampyclaw config validate
```

**Linux / macOS / WSL2** are supported. Requires Python **3.11+**. The
default LLM backend is [Ollama](https://ollama.ai/)
running on `127.0.0.1:11434` — install Ollama and pull a tool-capable
model, e.g.:

```bash
ollama pull gemma4:latest
```

Override the model with `--model <id>`. Tested models:

| Model | Context | Notes |
|---|---|---|
| `gemma4:latest` (= `e4b`) | 128K | Multimodal (text+image), native function calling, ~9.6 GB. |
| `gemma4:e2b` | 128K | Lighter (~7.2 GB) — same family, smaller. |
| `gemma4:26b` / `31b` | 256K | Heavier MoE variants when you have the RAM. |
| `qwen2.5:7b-instruct` | 32K | Strong tool calling. |
| `llama3.1:8b` | 128K | Broadly capable. |
| `mistral-nemo:12b` | 128K | Slower, more verbose. |

You can run sampyClaw with no LLM (RPC + tools only) by using
`--provider echo` for testing.

#### Internal vLLM server

If your team runs an internal vLLM (`vllm serve …`) box, point the
gateway at it directly — no Ollama required:

```bash
sampyclaw gateway start \
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

Create `~/.sampyclaw/config.yaml`:

```yaml
channels: {}     # populate per channel below
agents:
  default:
    id: default
    provider: local              # local | vllm | anthropic | pi | echo
    model: gemma4:latest
    system_prompt: |
      You are a helpful assistant.
```

### 2. Optional — connect Telegram

Get a bot token from [@BotFather](https://t.me/botfather) and write
`~/.sampyclaw/credentials/telegram/main.json`:

```json
{ "bot_token": "123456:ABC..." }
```

Add the binding to `config.yaml`:

```yaml
channels:
  telegram:
    main:
      enabled: true
```

### 3. Start the gateway

```bash
export SAMPYCLAW_GATEWAY_TOKEN=$(openssl rand -hex 32)
sampyclaw gateway start --provider local
```

The bundled dashboard is at `http://127.0.0.1:7331/` and Prometheus
metrics at `/metrics`. **Open the dashboard URL in any browser** — when
auth is configured, the page detects the missing token and renders an
in-app login gate. Paste the value of `SAMPYCLAW_GATEWAY_TOKEN`, click
*Connect*, and the dashboard remembers it for 12 hours via cookie +
`localStorage` so reloads need nothing extra.

You can also bypass the form entirely with
`http://127.0.0.1:7331/?token=<SAMPYCLAW_GATEWAY_TOKEN>` — the gateway
sets the cookie on first response and the dashboard JS strips the
token out of the address bar so it doesn't leak via screenshots or
browser history. `/healthz`, `/readyz`, `/metrics` always remain
unauthenticated for orchestrator probes.

Send a Telegram DM to your bot — it replies via the local model with
full tool access.

### 4. Send a one-off message via CLI

```bash
sampyclaw message send --agent default "summarize today's news headlines"
```

---

## Architecture

```
   ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
   │ Tauri desktop    │  │ Browser dashboard│  │ Channels         │
   │ (Win .msi /      │  │ (built-in SPA at │  │ - Telegram (in)  │
   │  Ubuntu .deb /   │  │  port 7331)      │  │ - Slack outbound │
   │  .AppImage)      │  │                  │  │ - …pluggable     │
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
   │ `--provider anthropic` → PiAgent pinned to claude-sonnet-4-6│
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
| `agents/` | Agent registry, factory, `LocalAgent` (Ollama / vLLM / OpenAI-compatible), `PiAgent` (22 providers via `pi/`, also serves `--provider anthropic`), `EchoAgent` |
| `channels/` | Channel abstraction, router, runner supervisor (restart-on-error with backoff) |
| `extensions/telegram/` | First-party Telegram plugin (aiogram) — file-for-file mirror of openclaw's TS module |
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
# ~/.sampyclaw/skills/ticket-lookup/ticket_lookup.py
from pydantic import BaseModel, Field
from sampyclaw.agents.tools import FunctionTool

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

`~/.sampyclaw/mcp.json` (same shape Claude Desktop / mcp-cli use):

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
sampyclaw message send --agent default "summarize my Slack DMs hourly"
# ... agent invokes the cron tool to register the job
```

### Add a custom channel

Drop a Python package with a `sampyclaw.plugins` entry point:

```toml
# pyproject.toml of your plugin
[project.entry-points."sampyclaw.plugins"]
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
- **`sampyclaw backup create/verify/restore`** with consistent SQLite
  snapshots even while the gateway runs.
- **`scripts/soak.py --duration 14400`** for pre-release stability
  validation (CSV trace + automatic memory/FD-leak threshold check).
- **`SAMPYCLAW_LOG_FORMAT=json`** for log aggregators (Loki/Datadog/…).
  Every log line carries the `trace_id` of the originating RPC.

---

## Status

| Component | Status |
|---|---|
| Core gateway, agent runtime, Telegram channel | ✅ Production-ready |
| Memory + sessions + wiki | ✅ |
| MCP **client** (import existing servers) | ✅ |
| Browser tools (BR-1, fail-closed Playwright) | ✅ Opt-in via `SAMPYCLAW_ENABLE_BROWSER=1` |
| Canvas tools (CV-1, dashboard-embedded HTML) | ✅ Opt-in via `SAMPYCLAW_ENABLE_CANVAS=1` |
| MCP **server** (expose sampyClaw to other clients) | ⏳ Future phase |
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

자체 호스팅 가능한 멀티 채널 AI 어시스턴트 게이트웨이. Python 구현.
사용자 자신의 모델 (로컬 Ollama, Anthropic, 또는 `pi` 런너로 22개
프로바이더) 을 메신저(Telegram 등)에 연결하고, 도구·스킬을 부여해서
프로덕션급 관측성을 갖춘 장기 실행 서비스로 운영할 수 있다.

> [openclaw](https://github.com/openclaw/openclaw)의 Python 포트 —
> 서버/CLI 부분만 추려서 자체 호스팅이 가능하도록 강화·문서화한 버전.

### 왜 sampyClaw?

| | |
|---|---|
| 🦙 **모델 자유** | 기본은 로컬 Ollama (도구 호출 가능한 모델 아무거나). Anthropic, OpenAI 호환, Bedrock, Google, Groq, DeepSeek, Mistral, Together, Fireworks 등 `pi` 통해 22개 프로바이더 지원. |
| 🖼️ **멀티모달 기본 지원** | Telegram에서 사진을 보내거나 **대시보드 chat에서 📎 버튼으로 첨부**하면 vision 가능 모델(gemma4 / Claude 3+ / GPT-4o / Gemini 1.5+ / llava 등)이 그 자리에서 본다. Vision 미지원 모델은 텍스트 컨텍스트에 "이미지 N장 드롭됨" 안내가 자동으로 들어간다. |
| 🖥️ **번들 대시보드 SPA** | 라이트/다크 테마 토글, Ctrl+K command palette, 세션 브라우저(리스트/미리보기/리셋/포크/아카이브), 모바일 반응형 drawer, in-app 로그인 게이트. 빌드 단계 없음, JSON-RPC 웹소켓과 동일 포트에서 서빙. |
| 💻 **네이티브 데스크톱 앱 (Windows + Ubuntu)** | Tauri 기반 클라이언트 — Windows 11 (`.msi`/`.exe`), Ubuntu 22.04 + 24.04 (`.deb`), 범용 Linux (`.AppImage`). OS 키체인 토큰 저장, 네이티브 토스트 알림, 시스템 트레이, Origin 제한 WS upgrade, Ed25519 서명 자동 업데이트. [`docs/DESKTOP_APP.md`](docs/DESKTOP_APP.md) 참고. |
| 🔌 **개방형 설계** | Plugin SDK + entry-point 자동 디스커버리. 새 채널·스킬은 `pip install`로 끝. |
| 🛡️ **프로덕션급 보안** | NetPolicy + DNS pinning + SSRF 가드, 도구 격리 실행 (RLIMIT + bwrap), 사람 승인 게이트, 서브프로세스 MCP 서버용 위험 env 스트립. |
| 📊 **운영을 진지하게** | Prometheus `/metrics`, `/healthz` + `/readyz`, RPC 단위 `trace_id` 가 박힌 구조화 JSON 로그, SIGTERM 그레이스풀 드레인, 온라인 SQLite 백업/복구. |
| 🧠 **내장 메모리** | sqlite-vec 벡터 스토어 + FTS5 + MMR 재정렬 + 임베딩 캐시. WAL 영속 세션, 자동 압축, 영속 지식 베이스 ("memory wiki"). |
| 🛠️ **확장 쉬움** | `SKILL.md` + Python 도구 두 파일이면 끝. 또는 `mcp.json`에 기존 MCP 서버 등록. |
| 🪞 **클라우드 종속 없음** | 노트북, Raspberry Pi, systemd 어디서든. 모든 상태는 `~/.sampyclaw/` 아래. |

### 설치

> **Windows 사용자?** WSL2 전용 가이드 참고:
> [`docs/INSTALL_WSL.md`](docs/INSTALL_WSL.md). Win32 네이티브는 미지원
> (샌드박스 + 시그널 + Linux 네트워킹 의존).

```bash
git clone https://github.com/andreason21/sampyClaw.git
cd sampyClaw
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

sampyclaw paths
sampyclaw config validate
```

**Linux / macOS / WSL2** 지원. Python **3.11+** 필요. 기본 LLM 백엔드는
[Ollama](https://ollama.ai/)
(`127.0.0.1:11434`). Ollama 설치 후 도구 호출 가능한 모델을 받는다, 예:

```bash
ollama pull gemma4:latest
```

기본값은 `--model <id>`로 오버라이드. 검증된 모델:

| 모델 | 컨텍스트 | 비고 |
|---|---|---|
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
sampyclaw gateway start \
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

`~/.sampyclaw/config.yaml`:

```yaml
channels: {}
agents:
  default:
    id: default
    provider: local
    model: gemma4:latest
    system_prompt: |
      You are a helpful assistant.
```

#### 2. (선택) Telegram 연결

[@BotFather](https://t.me/botfather)에서 봇 토큰 발급 →
`~/.sampyclaw/credentials/telegram/main.json`:

```json
{ "bot_token": "123456:ABC..." }
```

`config.yaml`에 바인딩 추가:

```yaml
channels:
  telegram:
    main:
      enabled: true
```

#### 3. 게이트웨이 실행

```bash
export SAMPYCLAW_GATEWAY_TOKEN=$(openssl rand -hex 32)
sampyclaw gateway start --provider local
```

번들 대시보드: `http://127.0.0.1:7331/`. Prometheus: `/metrics`.
**브라우저에서 대시보드 URL을 그냥 열기** — 인증이 설정된 상태면 SPA가
토큰 없음을 감지하고 화면 안에 로그인 폼을 띄움. `SAMPYCLAW_GATEWAY_TOKEN`
값을 붙여넣고 *Connect* — 12시간 쿠키 + `localStorage`에 저장되어 새로고침
시 별도 입력 불필요.

URL 한 방으로 끝내고 싶으면 `http://127.0.0.1:7331/?token=<SAMPYCLAW_GATEWAY_TOKEN>`
도 가능 — 게이트웨이가 응답에 쿠키를 설정하고 SPA가 주소창에서 토큰을
제거 (스크린샷·브라우저 히스토리 누출 방지). `/healthz`, `/readyz`,
`/metrics`는 오케스트레이터 프로브용으로 항상 비인증 유지.

Telegram DM을 보내면 로컬 모델이 도구를 사용해 답한다.

#### 4. CLI에서 일회성 메시지

```bash
sampyclaw message send --agent default "오늘 뉴스 헤드라인 요약해줘"
```

### 아키텍처

```
   ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
   │ Tauri 데스크톱앱 │  │ 브라우저 대시보드│  │ 채널              │
   │ (Win .msi /      │  │ (번들 SPA,       │  │ - Telegram (양방향)│
   │  Ubuntu .deb /   │  │  port 7331)      │  │ - Slack 아웃바운드│
   │  .AppImage)      │  │                  │  │ - …플러그인 가능  │
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
   │ `--provider anthropic` → PiAgent + claude-sonnet-4-6 기본값   │
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
| `agents/` | 에이전트 레지스트리·팩토리, LocalAgent / PiAgent / EchoAgent (`--provider anthropic`은 PiAgent로 라우팅) |
| `channels/` | 채널 추상화, 라우터, 슈퍼바이저(에러 시 백오프 재시작) |
| `extensions/telegram/` | 1st-party Telegram 플러그인 (aiogram). openclaw TS 모듈을 파일 단위로 미러링 |
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
# ~/.sampyclaw/skills/ticket-lookup/ticket_lookup.py
from pydantic import BaseModel, Field
from sampyclaw.agents.tools import FunctionTool

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

`~/.sampyclaw/mcp.json` (Claude Desktop / mcp-cli와 동일 스키마):

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

서버의 도구가 sampyClaw 네이티브 도구처럼 노출된다. 전체 시나리오
(설치 → 연결 → 직접 호출 → 에이전트 연결까지)를 검증한 예제는
[`docs/MCP_YAHOO_FINANCE.md`](docs/MCP_YAHOO_FINANCE.md) 참고.

#### 반복 작업 스케줄링

```bash
sampyclaw message send --agent default "매시간 Slack DM 요약해줘"
# 에이전트가 cron 도구를 호출해서 작업 등록
```

#### 커스텀 채널 추가

`sampyclaw.plugins` entry-point가 있는 Python 패키지 작성:

```toml
[project.entry-points."sampyclaw.plugins"]
discord = "my_pkg.discord_plugin:DISCORD_PLUGIN"
```

`pip install -e .` 후 게이트웨이 재시작이면 끝.

### 운영

프로덕션 배포 가이드: [`docs/OPERATIONS.md`](docs/OPERATIONS.md). 요점:

- **systemd 유닛** + SIGTERM 그레이스풀 종료
- **Prometheus 알람** RPC 에러율, p99 턴 지연, 승인 백로그 등
- **`sampyclaw backup create/verify/restore`** — 게이트웨이 가동 중에도
  일관된 SQLite 스냅샷
- **`scripts/soak.py --duration 14400`** 릴리스 전 안정성 검증
  (CSV trace + 메모리/FD 누수 임계 자동 검사)
- **`SAMPYCLAW_LOG_FORMAT=json`** 로그 집약기용. 모든 라인에
  발신 RPC의 `trace_id`가 박힌다.

### 상태

| 구성 요소 | 상태 |
|---|---|
| 코어 게이트웨이, 에이전트 런타임, Telegram | ✅ 프로덕션 가능 |
| 메모리 + 세션 + 위키 | ✅ |
| MCP **클라이언트** (외부 서버 흡수) | ✅ |
| 브라우저 도구 (BR-1, fail-closed Playwright) | ✅ `SAMPYCLAW_ENABLE_BROWSER=1` 옵트인 |
| 캔버스 도구 (CV-1, dashboard 임베드 HTML) | ✅ `SAMPYCLAW_ENABLE_CANVAS=1` 옵트인 |
| MCP **서버** (sampyClaw를 외부 클라이언트에 노출) | ⏳ 차후 |
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
