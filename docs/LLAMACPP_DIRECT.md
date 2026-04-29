# `llamacpp-direct` — managed `llama-server` provider

> **This is the default local-chat path** (since 2026-04-29). The CLI's
> `--provider auto` selects `llamacpp-direct` whenever
> `$OXENCLAW_LLAMACPP_GGUF` is set and a `llama-server` binary is
> reachable on `$PATH`; otherwise it falls back to `--provider ollama`
> with a one-line warning. Set `--provider llamacpp-direct` (or the
> legacy alias `--provider local`) to pin it.

oxenClaw can serve a local GGUF directly through `llama.cpp`'s
`llama-server` binary, with oxenClaw owning the lifecycle of the child
process. The same GGUF runs **noticeably faster than going through
Ollama** because the managed server is started with the
unsloth-studio-style fast preset:

- `--flash-attn on` (forced, not "best-effort")
- `--jinja` (the GGUF's own chat template; no Ollama Modelfile re-encode)
- `--no-context-shift` (fail fast at the ctx limit instead of silent slow-rotate)
- `-ngl 999` (offload every layer the model has to GPU)
- `--parallel 1` (single-slot decode is faster than multi-slot for one user)

Boot cost (mmap + VRAM upload) is paid once per GGUF and the server
stays warm between requests; switching GGUFs kills + restarts the child.

This sits **alongside** the existing `llamacpp` provider id, which
keeps assuming a `llama-server` you started yourself. Use
`llamacpp-direct` when you want oxenClaw to spawn it.

## Prerequisites

You need two things on disk: the `llama-server` binary and a GGUF file.

> **Quickest path: `oxenclaw setup llamacpp`** — a one-shot interactive
> wizard that downloads the binary (from a release URL you paste),
> downloads a GGUF (defaults to
> `unsloth/gemma-4-E4B-it-GGUF/gemma-4-E4B-it-UD-Q4_K_XL.gguf` —
> ~4.8 GiB, fits an 8 GiB GPU), persists both paths to
> `~/.oxenclaw/env`, and runs a CPU smoke test. Re-runnable: every
> step short-circuits if the prerequisite is already in place.
> `oxenclaw doctor` lights up the same warning when these aren't
> configured.

The manual recipe below is the same set of steps the wizard runs, in
case you want to script them yourself.

### 1. Install `llama-server`

The wizard's default path is **build from source** because it picks
the right backend for your machine (CUDA / Metal / Vulkan / CPU)
without you having to match driver versions to a prebuilt asset. Pick
whichever option below suits you.

#### Option A — git clone + cmake (recommended; what the wizard runs)

```bash
# Prereqs: git + cmake, plus the toolkit for your backend
#   • CUDA on Linux:   the matching `cuda-toolkit` package
#   • Metal on macOS:  Xcode CLT (`xcode-select --install`)
#   • Vulkan:          your distro's vulkan-sdk + glslc
git clone --depth 1 https://github.com/ggml-org/llama.cpp ~/.oxenclaw/llama.cpp
cd ~/.oxenclaw/llama.cpp

# Pick ONE backend flag (the wizard auto-detects this for you):
cmake -S . -B build -DGGML_CUDA=ON       # NVIDIA
# cmake -S . -B build -DGGML_METAL=ON      # Apple Silicon
# cmake -S . -B build -DGGML_VULKAN=ON     # Vulkan
# cmake -S . -B build                       # CPU only

cmake --build build --config Release --target llama-server -j
export OXENCLAW_LLAMACPP_BIN=$HOME/.oxenclaw/llama.cpp/build/bin/llama-server
$OXENCLAW_LLAMACPP_BIN --version                          # smoke-check
```

`~/.oxenclaw/llama.cpp/` is on the binary discovery path, so once the
build lands there `find_llama_server_binary()` picks it up without an
env var. Build time: ~5–15 min on CPU, much faster with CUDA / Metal
since `--target llama-server` skips the long tail of unused tools.

#### Option B — prebuilt release from `ggml-org/llama.cpp`

If you'd rather not build, the upstream project publishes prebuilt
zips per release. You have to match the asset to your CUDA driver /
glibc / OS yourself, which is the main reason Option A is the default.

```bash
# 1. Browse releases and copy the URL of the asset for your platform.
#    https://github.com/ggml-org/llama.cpp/releases
#    Linux x86_64 + CUDA:  llama-<ver>-bin-ubuntu-x64-cuda.zip
#    macOS arm64 (Metal):  llama-<ver>-bin-macos-arm64.zip
#    Windows x64 + CUDA:   llama-<ver>-bin-win-cuda-x64.zip

# 2. Extract somewhere stable, e.g. ~/.local/llama.cpp
mkdir -p ~/.local/llama.cpp && cd ~/.local/llama.cpp
curl -LO <release-asset-url>
unzip llama-*-bin-*.zip

# 3. Point oxenClaw at it.
export OXENCLAW_LLAMACPP_BIN=$HOME/.local/llama.cpp/build/bin/llama-server
```

#### Option C — package manager

```bash
brew install llama.cpp        # macOS (Metal)
sudo pacman -S llama.cpp      # Arch
# Debian/Ubuntu: no official package yet — use Option A.
```

`brew` and `pacman` builds are CPU-only or Metal; for CUDA on Linux
use Option A.

#### Where oxenClaw looks

`find_llama_server_binary()` resolves in this order:

1. `$OXENCLAW_LLAMACPP_BIN` (full path)
2. `$LLAMA_SERVER_PATH`
3. `shutil.which("llama-server")` — anything on `$PATH`
4. `~/.oxenclaw/llama.cpp/`, `~/.oxenclaw/llama.cpp/build/bin/`
5. `/usr/local/bin/`, `/opt/llama.cpp/bin/`

If none resolve, `LlamaCppServerError: llama-server binary not found`
is raised at first request — pick any path above and you're set.

### 2. Download a GGUF

Any HuggingFace GGUF works. Common picks:

```bash
mkdir -p ~/models
cd ~/models

# Hugging Face CLI (fastest if you already have huggingface_hub):
pip install -U "huggingface_hub[cli]"
hf download unsloth/gemma-4-E4B-it-GGUF \
    gemma-4-E4B-it-UD-Q4_K_XL.gguf --local-dir .

# Or curl from the resolve URL:
curl -L -o gemma-4-E4B-it-UD-Q4_K_XL.gguf \
    https://huggingface.co/unsloth/gemma-4-E4B-it-GGUF/resolve/main/gemma-4-E4B-it-UD-Q4_K_XL.gguf
```

Quantisation rule of thumb: **Q4_K_M** is the sweet spot for most
8–13 GB GPUs; **Q5_K_M** if you have headroom; **Q8_0** for small
models (< 4 B params) where size still fits.

Point oxenClaw at it:

```bash
export OXENCLAW_LLAMACPP_GGUF=$HOME/models/gemma-4-E4B-it-UD-Q4_K_XL.gguf
```

Or per-agent in `config.yaml`:

```yaml
agents:
  default:
    provider: llamacpp-direct
    extra:
      gguf_path: /home/me/models/gemma-4-E4B-it-UD-Q4_K_XL.gguf
```

## Configuration

One required setting (the GGUF path), everything else has a default.

| Knob | `model.extra` key | Env var | Default |
|---|---|---|---|
| GGUF path | `gguf_path` | `OXENCLAW_LLAMACPP_GGUF` | (required) |
| Context window | `n_ctx` | `OXENCLAW_LLAMACPP_CTX` | `32768` (drop to `16384` on tight VRAM) |
| GPU layers | `n_gpu_layers` | `OXENCLAW_LLAMACPP_NGL` | `999` |
| CPU threads | `n_threads` | `OXENCLAW_LLAMACPP_THREADS` | `-1` (auto) |
| Parallel slots | `n_parallel` | `OXENCLAW_LLAMACPP_PARALLEL` | `1` |
| Extra `llama-server` flags | `extra_args` (list) | `OXENCLAW_LLAMACPP_EXTRA_ARGS` (shell-split) | `()` |
| Chat template file | `chat_template` | `OXENCLAW_LLAMACPP_TEMPLATE` | — |
| Multimodal projector | `mmproj_path` | `OXENCLAW_LLAMACPP_MMPROJ` | — |
| `llama-server` binary | — | `OXENCLAW_LLAMACPP_BIN` (or `LLAMA_SERVER_PATH`) | from `$PATH` |
| Spawn-readiness timeout (s) | — | `OXENCLAW_LLAMACPP_HEALTH_TIMEOUT_S` | `600` |

`model.extra[<key>]` overrides the env var for the same setting, so a
specific `Model` registration can pin its own GGUF + context size while
the env defaults still cover ad-hoc runs.

## Quickstart

```bash
# 1. Make sure llama-server is reachable.
which llama-server   # or: export OXENCLAW_LLAMACPP_BIN=/opt/llama.cpp/build/bin/llama-server

# 2. Point at a GGUF you've already downloaded.
export OXENCLAW_LLAMACPP_GGUF=$HOME/models/qwen3-coder-30b-q4_k_m.gguf
export OXENCLAW_LLAMACPP_CTX=32768          # default 32768; set 16384 only on tight VRAM
export OXENCLAW_LLAMACPP_NGL=999            # optional, default 999

# 3. Start the gateway with the managed provider.
oxenclaw gateway start --provider llamacpp-direct --model local-gguf
```

The first request triggers `llama-server` to spawn on a free localhost
port; oxenClaw waits for `/health` (up to 600 s by default; large
models on cold mmap can take a while), then streams via the OpenAI-
compatible `/v1/chat/completions` endpoint. Subsequent requests hit
the warm server.

## Comparing to Ollama on the same model

| Property | Ollama path | `llamacpp-direct` |
|---|---|---|
| Model loading | Ollama Modelfile re-encode | direct GGUF mmap |
| Chat template | re-emitted by Ollama | model's native Jinja (`--jinja`) |
| `--flash-attn` | backend-dependent | forced `on` |
| GPU offload default | partial / heuristic | full (`-ngl 999`) |
| Context shift | silent slow-rotate | `--no-context-shift` (fast fail) |
| First-token latency | Ollama daemon hop + Modelfile parse | one mmap + one prefill |
| Parallel slots | `OLLAMA_NUM_PARALLEL` (shares VRAM) | `--parallel 1` (faster for single user) |

### Live measurement (2026-04-29)

Same machine (RTX 3050 8 GB, full GPU offload), same GGUF
(`gemma-4-E4B-it-UD-Q4_K_XL.gguf`, 4.8 GiB), same prompts, ctx=8192,
temperature=0:

| Metric | `llamacpp-direct` | `ollama` (native `/api/chat`) |
|---|---|---|
| Cold first-token | 3.50 s | 4.23 s |
| Warm first-token | 0.44 s | 0.41 s |
| **Warm decode rate** | **16.6 tok/s** | **5.6 tok/s** |

Both paths fully offload the model to GPU (`size_vram == size`); the
3× decode-rate gap is purely from the flag set above
(`--flash-attn on` + `--jinja` + `--no-context-shift` + `--parallel 1`).
This is why `--provider auto` prefers `llamacpp-direct` whenever it
can.

## Embeddings via `llama-server --embedding` (replaces Ollama)

`oxenclaw setup llamacpp` Step 3 optionally configures a **second**
managed `llama-server` instance dedicated to the memory pipeline's
embedding traffic. Once configured, you can fully unplug Ollama —
chat goes through the chat-server (Step 1/2), embeddings go through
the embed-server, both warm and on the same box.

Required env vars (the wizard writes them to `~/.oxenclaw/env`):

| Knob | Env var | Default |
|---|---|---|
| Embedding GGUF | `OXENCLAW_LLAMACPP_EMBED_GGUF` | (required; e.g. `nomic-ai/nomic-embed-text-v2-moe-GGUF/nomic-embed-text-v2-moe.Q4_K_M.gguf` — 328 MiB) |
| Embed ctx | `OXENCLAW_LLAMACPP_EMBED_CTX` | `8192` |
| Embed GPU layers | `OXENCLAW_LLAMACPP_EMBED_NGL` | `999` |
| Pooling strategy | `OXENCLAW_LLAMACPP_EMBED_POOLING` | model default (mean for nomic) |
| Switch embedder | `OXENCLAW_EMBEDDER` | wizard sets to `llamacpp-direct` |

The two `llama-server` instances run on **separate ephemeral ports**
managed by independent singletons (`get_default_server()` for chat,
`get_embedding_server()` for embeddings) — switching chat models
doesn't kick the embedding server, and vice versa.

VRAM accounting tip: a typical setup is gemma-4-E4B chat (~5 GiB) +
nomic-embed-text-v2-moe Q4 (~370 MiB) → both fit on an 8 GiB GPU
with room for ctx KV. If VRAM is tight, set
`OXENCLAW_LLAMACPP_EMBED_NGL=0` to keep embeddings on CPU (they're
small and embedding latency is rarely the bottleneck).

`oxenclaw doctor` reports the embedding-direct path as `embeddings:
[OK] llamacpp-direct embedder ready` once the GGUF + binary resolve.

## Failure modes

- **Binary missing.** `LlamaCppServerError: llama-server binary not found`.
  Set `$OXENCLAW_LLAMACPP_BIN` or install `llama.cpp` on `PATH`.
- **GGUF path missing.** `no GGUF path configured`. Set
  `$OXENCLAW_LLAMACPP_GGUF` or pass `model.extra['gguf_path']`.
- **Spawn / health timeout.** The error message contains the last
  ~30 lines of the child's stdout — that's where llama.cpp prints
  "key not found" or "unknown model architecture" when the GGUF is
  unparseable, or "VRAM exhausted" when `-c` is too high. Drop
  `OXENCLAW_LLAMACPP_CTX` or pick a smaller-quant GGUF.
- **Spec change between requests.** Switching `gguf_path` / `n_ctx` /
  `n_gpu_layers` mid-session triggers a kill + respawn. Same spec is
  a no-op (warm server reused).

## Wire-level debugging

The wire trace at `~/.oxenclaw/logs/llm-trace.jsonl` works exactly
like the Ollama path — the provider tags traces with
`provider: "llamacpp-direct"`. Enable with:

```bash
export OXENCLAW_LLM_TRACE=1
```

The trace records the *real* base URL (the managed port), the patched
payload (`stop: []` is stripped to keep older `llama-server` builds
happy), and the assembled response.
