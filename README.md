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
| 🦙 **Bring your own model** | Default is local Ollama (`gemma4:latest` — tool-capable, lightweight, **multimodal**). Anthropic, OpenAI-compatible, Bedrock, Google, Groq, DeepSeek, Mistral, Together, Fireworks, … 22 providers via `pi`. |
| 🖼️ **Multimodal in/out of the box** | Send a photo through Telegram and a vision-capable model (gemma4 / Claude 3+ / GPT-4o / Gemini 1.5+ / llava / etc.) sees it. Models without vision get a dropped-image notice in their text context. |
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
running on `127.0.0.1:11434` — install Ollama and pull the recommended
default model:

```bash
ollama pull gemma4:latest
```

`gemma4:latest` is the recommended default: native function calling,
small enough to run on a laptop, and the sampyClaw provider catalog
ships with a 32K context window entry for it. Other tested models —
override with `--model <id>`:

| Model | Context | Notes |
|---|---|---|
| `gemma4:latest` (= `e4b`) | **128K** | **Recommended default.** Multimodal (text+image), native function calling, ~9.6 GB. |
| `gemma4:e2b` | 128K | Lighter (~7.2 GB) — same family, smaller. |
| `gemma4:26b` / `31b` | **256K** | Heavier MoE variants when you have the RAM. |
| `qwen2.5:7b-instruct` | 32K | Strong tool calling alternative. |
| `llama3.1:8b` | 128K | Broadly capable. |
| `mistral-nemo:12b` | 128K | Slower, more verbose. |

> **There is no `gemma4:9b` tag.** Gemma 4's size variants are `e2b` /
> `e4b` / `26b` / `31b`. `gemma4:latest` resolves to `e4b` (effective 4B
> parameters). Avoid `gemma3:4b` — earlier-gemma tool support is
> unreliable (catalog marks it `supports_tools=False`).

You can run sampyClaw with no LLM (RPC + tools only) by using
`--provider echo` for testing.

---

## Quick start

### 1. Minimum config

Create `~/.sampyclaw/config.yaml`:

```yaml
channels: {}     # populate per channel below
agents:
  default:
    provider: local              # local | anthropic | pi | echo
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

The bundled dashboard is now at `http://127.0.0.1:7331/` and Prometheus
metrics at `/metrics`. Send a Telegram DM to your bot — it replies via
the local model with full tool access.

### 4. Send a one-off message via CLI

```bash
sampyclaw message send --agent default "summarize today's news headlines"
```

---

## Architecture

```
                    ┌────────────────────────────────────────────┐
                    │              GATEWAY (port 7331)           │
                    │  WS JSON-RPC + HTTP /metrics /healthz      │
                    │             /readyz / dashboard            │
                    └──────────┬──────────────────┬──────────────┘
                               │                  │
   ┌───────────────────────────┴──┐         ┌─────┴─────────┐
   │ Channel router               │         │ Agent runtime │
   │ (one supervisor per channel) │         │  - LocalAgent │
   │                              │         │  - PiAgent    │
   │ Telegram, ... pluggable      │         │  - Anthropic  │
   └─────────────┬────────────────┘         └─────┬─────────┘
                 │                                │
                 └────────────┬───────────────────┘
                              │
   ┌──────────────────────────┴──────────────────────────────┐
   │  Tools  ·  Skills  ·  MCP client  ·  Memory  ·  Wiki     │
   │  Approvals  ·  Cron  ·  NetPolicy  ·  Sandbox            │
   └──────────────────────────────────────────────────────────┘
```

| Layer | What lives there |
|---|---|
| `gateway/` | WS JSON-RPC server, HTTP routes (`/metrics`, `/healthz`, `/readyz`, dashboard), per-connection concurrency cap, bearer auth, graceful shutdown |
| `agents/` | Agent registry, factory, `LocalAgent` (Ollama / OpenAI-compatible), `AnthropicAgent`, `PiAgent` (22 providers via `pi/`), `EchoAgent` |
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
    }
  }
}
```

The server's tools become first-class tools the agent can call.

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
| MCP **server** (expose sampyClaw to other clients) | ⏳ Future phase |
| 5 additional channels (Discord, Slack, …) | ⏳ Future phase |
| Native mobile / desktop apps | ❌ Out of scope |
| Full React web UI | ❌ Bundled single-page dashboard only |

**Test suite: 898 pass / 9 skip.** See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
for the full module map.

---

## Documentation

| Document | Purpose |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Reference architecture extracted from openclaw |
| [`docs/INSTALL_WSL.md`](docs/INSTALL_WSL.md) | Windows install via WSL2 (English + Korean) |
| [`docs/PORTING_PLAN.md`](docs/PORTING_PLAN.md) | Phased roadmap (D → B → A → M → PROD) |
| [`docs/SUBSYSTEM_MAP.md`](docs/SUBSYSTEM_MAP.md) | What's ported / partial / out-of-scope |
| [`docs/AUTHORING_SKILLS.md`](docs/AUTHORING_SKILLS.md) | Build your own tools and skills |
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
| 🦙 **모델 자유** | 기본은 로컬 Ollama (`gemma4:latest` — 도구 호출 + **멀티모달** 지원, 경량). Anthropic, OpenAI 호환, Bedrock, Google, Groq, DeepSeek, Mistral, Together, Fireworks 등 `pi` 통해 22개 프로바이더 지원. |
| 🖼️ **멀티모달 기본 지원** | Telegram에서 사진 보내면 vision 가능 모델(gemma4 / Claude 3+ / GPT-4o / Gemini 1.5+ / llava 등)이 그 자리에서 본다. Vision 미지원 모델은 텍스트 컨텍스트에 "이미지 N장 드롭됨" 안내가 자동으로 들어간다. |
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
(`127.0.0.1:11434`). Ollama 설치 후 권장 기본 모델 받기:

```bash
ollama pull gemma4:latest
```

`gemma4:latest`이 권장 기본: 네이티브 함수 호출 지원, 노트북에서도 무난히
돌아가는 크기, sampyClaw 프로바이더 카탈로그에 32K 컨텍스트 윈도우로
등록됨. 다른 검증된 모델 — `--model <id>`로 오버라이드:

| 모델 | 컨텍스트 | 비고 |
|---|---|---|
| `gemma4:latest` (= `e4b`) | **128K** | **권장 기본.** 멀티모달(text+image), 네이티브 함수 호출, 약 9.6 GB. |
| `gemma4:e2b` | 128K | 더 가벼움 (약 7.2 GB) — 같은 계열의 작은 변종. |
| `gemma4:26b` / `31b` | **256K** | 고RAM 환경용 MoE 변종. |
| `qwen2.5:7b-instruct` | 32K | 강한 도구 호출 대안. |
| `llama3.1:8b` | 128K | 범용성 좋음. |
| `mistral-nemo:12b` | 128K | 느리지만 verbose. |

> **`gemma4:9b` 태그는 존재하지 않음.** Gemma 4의 사이즈 변종은 `e2b` /
> `e4b` / `26b` / `31b`. `gemma4:latest`는 `e4b` (effective 4B 파라미터)로
> 리졸브된다. `gemma3:4b`는 피하기 — 이전 gemma 도구 지원이 불안정
> (카탈로그에 `supports_tools=False`로 등록).

LLM 없이 RPC + 도구만 테스트하려면 `--provider echo` 사용.

### 빠른 시작

#### 1. 최소 설정

`~/.sampyclaw/config.yaml`:

```yaml
channels: {}
agents:
  default:
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
Telegram DM을 보내면 로컬 모델이 도구를 사용해 답한다.

#### 4. CLI에서 일회성 메시지

```bash
sampyclaw message send --agent default "오늘 뉴스 헤드라인 요약해줘"
```

### 아키텍처

```
                    ┌────────────────────────────────────────────┐
                    │           GATEWAY (port 7331)              │
                    │  WS JSON-RPC + HTTP /metrics /healthz      │
                    │            /readyz / dashboard             │
                    └──────────┬──────────────────┬──────────────┘
                               │                  │
   ┌───────────────────────────┴──┐         ┌─────┴─────────┐
   │ 채널 라우터                  │         │ 에이전트 런타임│
   │ (채널당 슈퍼바이저 1개)      │         │  - LocalAgent │
   │ Telegram 등 플러그인 가능     │         │  - PiAgent    │
   └─────────────┬────────────────┘         │  - Anthropic  │
                 │                          └─────┬─────────┘
                 └──────────┬───────────────────┘
                            │
   ┌────────────────────────┴────────────────────────────────┐
   │  도구 · 스킬 · MCP 클라이언트 · 메모리 · 위키            │
   │  승인 · cron · NetPolicy · 샌드박스                     │
   └──────────────────────────────────────────────────────────┘
```

| 계층 | 하는 일 |
|---|---|
| `gateway/` | WS JSON-RPC 서버, HTTP 라우트, 연결당 동시성 한도, Bearer 인증, 그레이스풀 종료 |
| `agents/` | 에이전트 레지스트리·팩토리, LocalAgent / AnthropicAgent / PiAgent / EchoAgent |
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
    }
  }
}
```

서버의 도구가 sampyClaw 네이티브 도구처럼 노출된다.

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
| MCP **서버** (sampyClaw를 외부 클라이언트에 노출) | ⏳ 차후 |
| 추가 채널 5종 (Discord, Slack, …) | ⏳ 차후 |
| 네이티브 모바일·데스크톱 앱 | ❌ 범위 외 |
| 풀 React 웹 UI | ❌ 번들 단일 페이지 대시보드만 |

**테스트: 898 pass / 9 skip.**

### 문서

| 문서 | 용도 |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | openclaw에서 추출한 레퍼런스 아키텍처 |
| [`docs/INSTALL_WSL.md`](docs/INSTALL_WSL.md) | Windows WSL2 설치 가이드 (영문 + 한글) |
| [`docs/PORTING_PLAN.md`](docs/PORTING_PLAN.md) | 단계별 로드맵 (D → B → A → M → PROD) |
| [`docs/SUBSYSTEM_MAP.md`](docs/SUBSYSTEM_MAP.md) | 포팅 / 부분 / 범위외 분류 |
| [`docs/AUTHORING_SKILLS.md`](docs/AUTHORING_SKILLS.md) | 자기 도구·스킬 작성 가이드 |
| [`docs/SECURITY.md`](docs/SECURITY.md) | 위협 모델 + 다층 방어 |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | 설치 · 실행 · 관측 · 백업 · 복구 |
| [`docs/CONFIG_EXAMPLE.yaml`](docs/CONFIG_EXAMPLE.yaml) | 주석 달린 설정 샘플 |
| [`docs/MEMORY_COMPARISON.md`](docs/MEMORY_COMPARISON.md) | 메모리 서브시스템 vs openclaw |

### 라이선스

MIT.
