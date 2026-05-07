# Ollama tuning guide

> **As of 2026-04-29, `llamacpp-direct` is the recommended local path
> for chat** — it runs the same GGUF roughly **3× faster** in our live
> RTX 3050 measurement (16.6 tok/s vs 5.6 tok/s, see
> [`LLAMACPP_DIRECT.md`](./LLAMACPP_DIRECT.md)). The CLI's
> `--provider auto` (the new default) automatically picks
> `llamacpp-direct` when it's configured, and silently falls back to
> the Ollama path documented below when it isn't, so this guide stays
> relevant for two cases:
>
> 1. You're using Ollama for **embeddings** (`nomic-embed-text` for
>    memory features). The `llamacpp-direct` provider can now also
>    serve embeddings via a second managed `llama-server --embedding`
>    instance, so this is no longer a hard requirement — see
>    [`LLAMACPP_DIRECT.md` § Embeddings](./LLAMACPP_DIRECT.md#embeddings-via-llama-server---embedding-replaces-ollama)
>    for the unplug-Ollama path. The wizard's `oxenclaw setup llamacpp`
>    Step 3 wires it up in one shot.
> 2. You explicitly want the Ollama chat path because you prefer its
>    "pull a name and go" model management.

oxenClaw talks to Ollama through the **native `/api/chat` provider**
(`oxenclaw/pi/providers/ollama.py`), not Ollama's OpenAI compatibility
shim. The shim silently caps `num_ctx` at 4096, truncates large prompts,
and drops streamed `tool_calls` deltas — every one of those is a bug
oxenClaw used to hit before 2026-04-29. The native path exposes the
full `options` surface, so the only knob that matters in practice is
**`num_ctx`** — how big a context window Ollama allocates per request.

## Why `num_ctx` matters

Each request you send Ollama is processed against a KV cache sized for
`num_ctx` tokens. The system prompt oxenClaw assembles can be very
large once you have memory facts, installed-skill manifests, and a long
conversation history — easily 10–30 KiB of text, which translates to
several thousand tokens. If `num_ctx` is too small, Ollama silently
**truncates the head of the prompt**, which is exactly where the tool
schemas live, and the model falls back to plain text instead of issuing
real tool calls. Symptom: the assistant emits `skill_run(...)` as
*text* in the body of its reply, not as a structured `tool_calls`
field.

The native provider's default is `num_ctx=32768`, which fits virtually
every real prompt and is safe on 16-24 GiB GPU / 32 GiB CPU machines.
Bump it only if `OXENCLAW_LLM_TRACE=1` shows you're hitting the cap.

## Knobs

`OXENCLAW_OLLAMA_NUM_CTX` controls the value the provider sends in
`options.num_ctx` on every request:

| Value | Behaviour |
|---|---|
| *(unset)* | `32768` — the default. |
| Any integer (e.g. `49152`) | Used verbatim. No upper cap, so the operator owns the OOM risk. |
| `auto` | On the first request for each model id, query `/api/show` and use `min(model_max, 32768)`. The cap matches the default deliberately — `auto` only lowers `num_ctx` for models whose advertised max is below the default; it never raises it. Bumping above `32768` is an explicit-integer-only decision because cold-allocating a 65 K+ KV cache on a 16 GB machine pegs Ollama for minutes and starves embedding traffic on the same server. |

The `auto` resolution is cached per process per model id, so the extra
`/api/show` round trip happens once.

## Estimating memory cost

KV cache size for one request is roughly:

```
KV bytes ≈ num_ctx × attn_layers × kv_heads × (key_length + value_length) × bytes_per_value
```

where `attn_layers = block_count / full_attention_interval` (SSM
hybrids only allocate KV on every Nth layer). For FP16 KV cache
(`bytes_per_value = 2`):

| Model | attn layers | kv_heads | key_len | num_ctx=16K | num_ctx=32K | num_ctx=65K | num_ctx=max |
|---|---|---|---|---|---|---|---|
| `qwen3.5:9b` (262 144 max, SSM-hybrid, interval=4) | 8 | 16 | 256 | ~2 GiB | ~4 GiB | ~8 GiB | ~32 GiB |
| `gemma4:latest` (131 072 max, GQA) | 12 | 2 | 512 | ~0.75 GiB | ~1.5 GiB | ~3 GiB | ~6 GiB |

The 8 GiB / 32 GiB column for `qwen3.5:9b` is the trap: cold-loading
that KV cache on a machine with 16 GB total RAM serialises every other
Ollama call (chat, embeddings) for the duration of the allocation and
breaks `memory.search` timeouts elsewhere in the gateway. That's why
`auto` caps at 32 K and bumping further is an explicit-integer-only
decision.

The provider logs an `info`-level estimate the first time it resolves
`num_ctx=auto` for a model, so you can see what you signed up for:

```
ollama qwen3.5:9b: num_ctx=auto resolved to 65536 (~8.0 GiB KV cache)
```

The estimate honours `attention.key_length` / `value_length`,
`full_attention_interval` (SSM hybrids), and GQA
(`head_count_kv`) when `/api/show` reports them. When fields are
missing it returns `None` and the log line is suppressed rather than
print a wrong number.

## Inspecting a model

```bash
# advertised context length + GQA shape
ollama show qwen3.5:9b

# full model_info dict the provider parses
curl -s http://127.0.0.1:11434/api/show -d '{"name":"qwen3.5:9b"}' | head -60
```

## Diagnostic recipe

Two env vars together let you see whether truncation is biting:

```bash
export OXENCLAW_LLM_TRACE=1                   # log every wire call
export OXENCLAW_OLLAMA_NUM_CTX=auto           # detect-and-cap

oxenclaw gateway start ...
# ... drive a chat that should hit a tool ...

# Latest request/response pair:
tail -2 ~/.oxenclaw/logs/llm-trace.jsonl | python3 -m json.tool | less
```

Look for:

- `payload.options.num_ctx` — what we asked Ollama for.
- `usage.prompt_tokens` (response) — what Ollama actually billed. If
  this lines up with your `num_ctx` value, the prompt fit. If it's
  pegged near a smaller round number (4096, 8192), Ollama capped you
  somewhere and `num_ctx` didn't take effect (most often: you're still
  on the OpenAI shim, or you set `OXENCLAW_OLLAMA_NUM_CTX` to a value
  smaller than the real prompt).
- `tool_calls` (response) — when populated, the model used the tool
  channel. When empty *and* `content` carries a `skill_run(...)` /
  fenced-JSON block, the model was forced into text mode by
  truncation. Bump `num_ctx` and retry.

## Picking a value

- **Default (32 768)** — leave it alone unless tracing shows you're
  losing tokens.
- **`auto`** — best ergonomics on a single-model deployment with
  comfortable RAM headroom. The 65 536 cap stops you from booking a
  model's full advertised window for routine traffic.
- **Explicit integer** — what to use when you know exactly how much
  context you need (long-form RAG, document QA, replay) and you've
  measured KV cache headroom against the formula above. `49152`,
  `65536`, `98304` are common round values.

Don't set this to the model's max "just in case." A 9B model at
262 144 ctx costs ~36 GiB of KV cache and Ollama allocates it
**up-front when the model loads**, even for one-line prompts.

## gemma3 / gemma4 function calling — full setup

### Why a custom Modelfile

Out of the box, Ollama's `gemma4:latest` ships a passthrough chat
template (`TEMPLATE {{ .Prompt }}`) — its built-in `RENDERER gemma4`
does not inject the tool list into the prompt or the
`<tool_call>...</tool_call>` markup the model expects. A plain
`gemma4` model never emits tool calls in oxenClaw even when the
prompt explicitly tells it to. The fix is a Modelfile that overrides
TEMPLATE so the user turn carries (a) the tool list, (b) the exact
JSON-line shape the parser wants, and (c) an explicit "MUST emit"
directive.

Live measurement, 4-task tool-calling bench:

| | tool calls | correct answers |
|---|---|---|
| plain `gemma4`         |  0/16 |  3/16 |
| `gemma4-fc` (this doc) | 16/16 | 14/16 |

A separate streaming-parser bug surfaced on the same bench: gemma3/4
return `tool_calls` in the FIRST streamed frame (`done:false`) rather
than in the final `done` frame. Fixed in
`oxenclaw/pi/providers/ollama.py` by hoisting the `tool_calls`
extraction out of the done branch so it runs per-frame. Without that
fix, the Modelfile change alone is invisible to oxenClaw — Ollama
returns the tool calls but the parser drops them. If you fork the
provider, replicate the per-frame behaviour.

### Build it (3 steps)

The full Modelfile lives at
[`scripts/modelfiles/gemma4-fc.Modelfile`](../scripts/modelfiles/gemma4-fc.Modelfile)
in this repo — copy-pasteable, complete, including the deterministic
decoding parameters that the bench used.

```bash
# 1. From the repo root, build the model. `-f` points at the file;
#    the first arg is the local model name Ollama will register.
ollama create gemma4-fc -f scripts/modelfiles/gemma4-fc.Modelfile

# 2. Verify the build registered:
ollama show --modelfile gemma4-fc | head -5
# Expect: `FROM <blob path>` followed by the TEMPLATE block.

# 3. Sanity-check the model loads + answers a trivial prompt:
ollama run gemma4-fc "Reply with exactly one word: ok"
```

If step 1 fails with "model not found: gemma4:latest", run
`ollama pull gemma4:latest` first — the FROM line in the Modelfile
expects the base model to already exist locally.

### Wire it into oxenClaw

CLI:

```bash
oxenclaw gateway start \
    --provider ollama \
    --model gemma4-fc \
    --base-url http://127.0.0.1:11434
```

Or in `config.yaml` (under the agent block whose model you want to
swap — `assistant` for the default):

```yaml
agents:
  assistant:
    provider: ollama
    model: gemma4-fc
    base_url: http://127.0.0.1:11434
```

### Verify the tool-call path end-to-end

After restarting the gateway with `gemma4-fc`, send a message that
must call a tool. With the dashboard:

1. Skills → Browse → install any knowledge-style skill that needs
   shell (e.g. `yahoo-finance-cli`) with the "관련 툴 자동 설치"
   checkbox on. (Requires `OXENCLAW_GATEWAY_BIN_AUTO_INSTALL=1` and
   `OXENCLAW_ASSISTANT_SHELL=1` — see the `feat(assistant): opt-in
   shell tool …` commit for details.)
2. In a chat session, ask a question that triggers the skill —
   e.g. `삼성전자 주가 알려줘`.
3. In the gateway log, you should see a line like:

   ```
   INFO oxenclaw.agents.dispatch: ... tool_call name=shell …
   ```

   That line is the smoke test. If you see `turn done` with
   `yielded=1` but no `tool_call` line in the same trace_id, the
   model never emitted a call — most often because the wrong model
   id is wired (plain `gemma4`, not `gemma4-fc`) or the gateway
   hasn't been restarted after the build.

### gemma3 — same recipe

Change the FROM line:

```
FROM gemma3:latest
```

…and rebuild as `gemma3-fc`. Everything else (TEMPLATE, RENDERER,
PARSER, PARAMETER lines) stays identical. Not bundled as a separate
file in this repo to avoid shipping two near-identical Modelfiles;
copy `gemma4-fc.Modelfile`, swap one line, run `ollama create
gemma3-fc -f <path>`.

### Troubleshooting

| Symptom | Likely cause |
|---|---|
| `ollama create` errors with `model not found: gemma4:latest` | Run `ollama pull gemma4:latest` first. |
| Gateway boots but no `tool_call` lines in the log | Gateway is still wired to plain `gemma4`. Restart with `--model gemma4-fc`. |
| Tool call fires but argument JSON is malformed (parser errors) | Check `temperature` / `top_k` / `top_p` parameters — `gemma4-fc` uses 0 / 1 / 0.95. Higher temperature drops arg JSON quality sharply on this 4B model. |
| Same model works in `ollama run` but oxenClaw never calls tools | Most likely the per-frame extraction fix didn't make it into your fork of `oxenclaw/pi/providers/ollama.py`. See the "streaming-parser bug" note above. |

