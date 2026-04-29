# Ollama tuning guide

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
| `auto` | On the first request for each model id, query `/api/show` and use `min(model_max, 65536)`. The cap stops a 262 144-window model from accidentally allocating 30+ GiB of KV cache. |

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

| Model | attn layers | kv_heads | key_len | num_ctx=32K | num_ctx=65K | num_ctx=max |
|---|---|---|---|---|---|---|
| `qwen3.5:9b` (262 144 max, SSM-hybrid, interval=4) | 8 | 16 | 256 | ~4 GiB | ~8 GiB | ~32 GiB |
| `gemma4:latest` (131 072 max, GQA) | 12 | 2 | 512 | ~1.5 GiB | ~3 GiB | ~6 GiB |

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
