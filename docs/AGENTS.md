# Agent configuration guide

oxenClaw is **on-host only**. The catalog ships exactly five providers,
all targeting local or LAN inference servers. Cloud / aggregator
providers (Anthropic, OpenAI, Google, Bedrock, OpenRouter, …) were
removed from the bundled catalog on 2026-04-29 — plugins can still add
their own provider id, but nothing hosted is shipped by default.

| Provider id | What it talks to | Default URL | When to pick it |
|---|---|---|---|
| `ollama` | the Ollama daemon (native `/api/chat`) | `http://127.0.0.1:11434/v1` | "pull a model name and go" UX, also the embeddings backend |
| `llamacpp-direct` | a `llama-server` child process **oxenClaw spawns itself** | managed (ephemeral port) | fastest decode (~3× Ollama on the same GGUF), see [`LLAMACPP_DIRECT.md`](./LLAMACPP_DIRECT.md) |
| `llamacpp` | an externally-managed `llama-server` | `http://127.0.0.1:8080/v1` | you already run `llama-server` yourself and just want oxenClaw to talk to it |
| `vllm` | `vllm serve …` (OpenAI-compat) | `http://127.0.0.1:8000/v1` | datacenter / multi-GPU vLLM box |
| `lmstudio` | LM Studio's local server (OpenAI-compat) | `http://127.0.0.1:1234/v1` | desktop GUI workflow on macOS / Windows |

The `--provider auto` default at the CLI picks `llamacpp-direct` when
both `$OXENCLAW_LLAMACPP_GGUF` is set and a `llama-server` binary is
on `$PATH`, and silently falls back to `ollama` otherwise. Set
`--provider <id>` (or `provider: <id>` in `config.yaml`) to pin one
explicitly.

---

## Where the config lives

`~/.oxenclaw/config.yaml` — a single YAML file with three top-level
sections relevant to agents:

```yaml
channels: {}      # how to receive messages (Slack, dashboard, …)
providers: {}     # optional per-provider overrides keyed by provider id
agents:           # the actual agent definitions
  default:
    provider: auto
    model: qwen3.5:9b
    system_prompt: |
      You are a helpful assistant.
```

`agents` is a dict — the **dict key** is the agent id, used everywhere
(`agent_id="default"` in CLI, `dispatcher.route(...)`, channel
binding, dashboard sessions). Multiple agents are encouraged: one for
chat, one for code, one for ops, etc.

The full Pydantic schema is in `oxenclaw/plugin_sdk/config_schema.py`
(`AgentConfig`, `RootConfig`); fields beyond the documented set are
allowed (`extra="allow"`) so plugin-specific knobs don't need a schema
bump.

---

## System-prompt composition

When `system_prompt` is omitted (or set to `null`) the agent assembles
one from a section catalog at `oxenclaw/agents/prompts/builder.py`.
Sections are conditionally injected based on what the agent actually
has loaded:

- **Always on**: identity line, `tool_use` shape rule, time / freshness reminder.
- **Per-tool playbooks**: `memory_save` adds the memory rules; `skill_run`
  adds skill discovery + the compressed anti-refusal; `weather`,
  `web_search`, `wiki_search` each add their playbook. Sections for
  tools you didn't load are skipped — saves tokens on trimmed
  deployments.
- **Model-family overlay**: a stronger "act, don't describe" tool-use
  enforcement is appended for non-thinking small local models
  (`qwen3.5`, `qwen2.5`, `llama3`, `gemma3/4`, `mistral`, `phi3/4`,
  `deepseek-coder`). Frontier models (`claude*`, `gpt-5`, `gpt-4o`,
  `gemini-2.x`) and thinking variants (`*-thinking`, `qwq`,
  `deepseek-r1`) skip it — they handle it natively and the extra
  ~150 tokens are wasted.
- **Channel hint**: when the gateway tags a delivery with a known
  channel id (`slack`, `discord`, `telegram`, `whatsapp`, `signal`,
  `email`), a markdown / media-delivery hint is appended so the
  model doesn't paste `**bold**` into a WhatsApp thread.

Section order is stable across calls so an upstream prompt cache
matches the prefix on every turn. Pass an explicit
`system_prompt: "..."` to opt out and supply your own.

---

## Minimum agent block

```yaml
agents:
  default:
    provider: auto          # auto | ollama | llamacpp-direct | llamacpp | vllm | lmstudio
    model: qwen3.5:9b       # provider-specific model id (see below)
    system_prompt: |
      You are a helpful assistant. Be concise.
```

That's the whole spec. `provider` + `model` + `system_prompt` is
enough to start the gateway and hold a conversation.

### All recognised fields

```yaml
agents:
  <agent_id>:
    # ── Required-ish ─────────────────────────────────────────────
    provider: auto              # picks llamacpp-direct or ollama
    model: qwen3.5:9b           # provider-native model id
    system_prompt: |            # inline string, or use @path syntax (see below)
      You are …

    # ── Endpoint overrides ───────────────────────────────────────
    base_url: http://gpu.lan:8000/v1   # only meaningful for vllm/lmstudio/llamacpp
    api_key: ${MY_INTERNAL_KEY}        # for vllm started with --api-key

    # ── Per-channel routing ──────────────────────────────────────
    channels:                          # which channels can reach this agent
      slack:
        allow_from: ["U01ABCD", "C99XYZ"]   # user/channel ids; empty = wide-open
      dashboard: {}                    # always allow

    # ── Provider-specific knobs ──────────────────────────────────
    extra:
      gguf_path: /home/me/models/qwen.gguf   # llamacpp-direct
      n_ctx: 32768                           # llamacpp-direct
      n_gpu_layers: 999                      # llamacpp-direct
```

Anything under `extra:` is forwarded to `Model.extra` and consumed by
the provider wrapper. The `llamacpp-direct` keys are documented in
[`LLAMACPP_DIRECT.md`](./LLAMACPP_DIRECT.md); `ollama` reads
`OXENCLAW_OLLAMA_NUM_CTX` from the environment instead.

---

## Per-provider examples

### 1. `ollama`

The friendliest setup. Ollama provides embeddings
(`nomic-embed-text`) for memory features by default — but if you'd
rather not depend on Ollama at all, the `llamacpp-direct` provider
can serve embeddings too. See
[`LLAMACPP_DIRECT.md` § Embeddings](./LLAMACPP_DIRECT.md#embeddings-via-llama-server---embedding-replaces-ollama)
or just run `oxenclaw setup llamacpp` and accept Step 3.

```bash
ollama pull qwen3.5:9b
ollama pull nomic-embed-text
```

```yaml
agents:
  default:
    provider: ollama
    model: qwen3.5:9b
    system_prompt: |
      You are a helpful assistant.
```

Tested chat models: `qwen3.5:9b` (default), `gemma4:latest`,
`gemma4:e2b`, `qwen2.5:7b-instruct`, `llama3.1:8b`. Full list +
sizing: [`OLLAMA.md`](./OLLAMA.md).

### 2. `llamacpp-direct` (recommended for chat)

oxenClaw spawns its own `llama-server` with the unsloth-studio fast
preset. Live measurement: 16.6 tok/s vs Ollama's 5.6 tok/s on the same
RTX 3050 + same Q4_K_XL GGUF.

**Two manual prerequisites.** The fastest way to handle both at once
is the wizard:

```bash
oxenclaw setup llamacpp
```

It downloads the binary (from a release URL you paste), downloads a
GGUF (default `unsloth/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-UD-Q4_K_XL.gguf`),
writes both paths to `~/.oxenclaw/env`, and runs a smoke test. Skip
the rest of this section if you ran the wizard. Otherwise the manual
quick version:

```bash
# 1. Install llama.cpp. Easiest paths (in order of preference):
#    • git clone + cmake build (auto-picks CUDA / Metal / Vulkan / CPU);
#      this is what `oxenclaw setup llamacpp` does for you.
#    • prebuilt zip from https://github.com/ggml-org/llama.cpp/releases
#    • `brew install llama.cpp` (macOS)
which llama-server                          # or: export OXENCLAW_LLAMACPP_BIN=/abs/path

# 2. Download a GGUF you want to serve (any HF GGUF works).
hf download unsloth/gemma-4-E4B-it-GGUF \
    gemma-4-E4B-it-UD-Q4_K_XL.gguf --local-dir ~/models
ls ~/models/gemma-4-E4B-it-UD-Q4_K_XL.gguf
```

```yaml
agents:
  coder:
    provider: llamacpp-direct
    model: local-gguf            # any label — weights are picked by gguf_path
    system_prompt: |
      You are an expert programmer. Write tight, well-tested code.
    extra:
      gguf_path: /home/me/models/qwen3-coder-30b-q4.gguf
      n_ctx: 32768               # default 32768
      n_gpu_layers: 999          # default 999 = full GPU offload
```

Or via env vars (handy when you only run one model at a time):

```bash
export OXENCLAW_LLAMACPP_GGUF=$HOME/models/qwen3-coder-30b-q4.gguf
export OXENCLAW_LLAMACPP_CTX=32768
oxenclaw gateway start          # --provider auto picks llamacpp-direct
```

Spec change between requests (different `gguf_path` / `n_ctx`)
triggers a kill + respawn. Same spec is a no-op (warm server reused).

### 3. `llamacpp` (external server)

You ran `llama-server` yourself; oxenClaw just talks to it via the
OpenAI-compat endpoint.

```bash
llama-server -m /home/me/models/x.gguf -c 16384 --port 8080 --jinja --flash-attn on
```

```yaml
agents:
  default:
    provider: llamacpp
    model: any-label
    base_url: http://127.0.0.1:8080/v1
    system_prompt: |
      You are …
```

If you want oxenClaw to manage that lifecycle for you, switch to
`llamacpp-direct` instead — same server, less orchestration on your
side.

### 4. `vllm`

```bash
vllm serve meta-llama/Llama-3.1-8B-Instruct --port 8000 --api-key $VLLM_API_KEY
```

```yaml
agents:
  default:
    provider: vllm
    model: meta-llama/Llama-3.1-8B-Instruct
    base_url: http://127.0.0.1:8000/v1
    api_key: ${VLLM_API_KEY}     # env var substitution; only required if vLLM was --api-key'd
    system_prompt: |
      You are …
```

### 5. `lmstudio`

LM Studio → "Developer" → "Start Server". It binds `127.0.0.1:1234`
by default.

```yaml
agents:
  default:
    provider: lmstudio
    model: qwen2.5-coder-32b-instruct  # whatever LM Studio shows as the model id
    system_prompt: |
      You are …
```

---

## Multiple agents

Define several entries under `agents:`; each gets its own id you can
target from channels and the dashboard.

```yaml
agents:
  default:
    provider: auto
    model: qwen3.5:9b
    system_prompt: |
      You are a friendly general assistant.
  coder:
    provider: llamacpp-direct
    model: local-gguf
    system_prompt: |
      You are a code review specialist. Be terse.
    extra:
      gguf_path: /home/me/models/qwen3-coder-30b-q4.gguf
  ops:
    provider: ollama
    model: gemma4:26b
    system_prompt: |
      You are an SRE. Prefer concrete commands.
```

The CLI flag `--agent-id <id>` (and the dashboard's session selector)
choose which entry receives a request.

---

## Channel → agent routing

Each channel binding can declare which agent handles its messages.
The minimum (one channel, one agent, no allow-list) is just:

```yaml
channels:
  slack:
    accounts:
      - account_id: main
        display_name: "Workspace Alerts"
agents:
  default:
    provider: auto
    model: qwen3.5:9b
    system_prompt: |
      You are …
    channels:
      slack: {}        # empty mapping = accept all from this channel
```

Restrict to specific Slack users or channel ids with `allow_from`:

```yaml
agents:
  default:
    channels:
      slack:
        allow_from:
          - "U01ABCDEFGH"   # user id
          - "C09ZYXWVUTS"   # channel id
```

---

## System prompts from a file

Inline `system_prompt:` is fine for short prompts. For long ones,
use `@path` to load from disk; relative paths resolve against
`~/.oxenclaw/`.

```yaml
agents:
  default:
    provider: auto
    model: qwen3.5:9b
    system_prompt: "@prompts/default.md"      # ~/.oxenclaw/prompts/default.md
```

The file is read once at gateway start. Restart the gateway after
edits (or use the dashboard's "reload config" RPC).

---

## Env var substitution in YAML

`${VAR}` in any string field is substituted from the environment at
load time:

```yaml
agents:
  default:
    provider: vllm
    model: meta-llama/Llama-3.1-8B-Instruct
    base_url: ${VLLM_URL}
    api_key: ${VLLM_API_KEY}
```

Missing vars expand to empty strings — set them via `~/.oxenclaw/env`,
`/etc/environment`, or a systemd `EnvironmentFile=` directive
depending on how you run the gateway.

---

## Tools and skills

Every agent gets the **default tool bundle** automatically:
`echo`, `get_time`, `read_file`, `list_dir`, `grep`, `glob`,
`read_pdf`, `write_file`, `edit`, `shell`, `process`, `update_plan`.

Optional bundles are env-gated (off by default):

```bash
export OXENCLAW_ENABLE_BROWSER=1   # browser_navigate / _click / _fill / _screenshot
export OXENCLAW_ENABLE_CANVAS=1    # canvas_present / canvas_hide
```

Skills (model-callable workflows) live under `~/.oxenclaw/skills/` —
each one is a directory with a `SKILL.md` manifest plus optional
Python tool. See `docs/SKILLS.md` for the authoring guide.

MCP servers are loaded from `~/.oxenclaw/mcp.json`; their tools are
namespaced and offered to every agent unless filtered.

---

## Sanity checks

```bash
# Validate config.yaml without starting the gateway.
oxenclaw config validate

# Show resolved provider info (default URL, env var, default model).
oxenclaw setup provider auto
oxenclaw setup provider llamacpp-direct
oxenclaw setup provider ollama

# Start with verbose logs to confirm provider resolution.
oxenclaw gateway start --verbose
```

The `auto` resolver logs which path it picked at startup; if you
expected `llamacpp-direct` and got `ollama`, the binary or GGUF env
var wasn't reachable — `oxenclaw setup provider llamacpp-direct` will
say so.

---

## Migrating from older configs

| Pre-rc.15 spelling | Now | Notes |
|---|---|---|
| `provider: local` | `provider: auto` | `local` is still accepted as a legacy alias and routes through `auto`. |
| `provider: pi` | `provider: auto` | same as above. |
| `provider: anthropic` / `openai` / `google` / `bedrock` / `openrouter` / `moonshot` / `zai` / `minimax` | (removed) | Cloud providers were removed from the bundled catalog. Plugins can re-add specific ones; the on-host catalog is the supported surface. |

Pre-rc.15 configs that named `local` or `pi` keep working unchanged —
the resolver picks `llamacpp-direct` when it's configured and
`ollama` when it isn't, so existing Ollama-only installs see no
regression.
