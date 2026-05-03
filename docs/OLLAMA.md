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

## gemma3 / gemma4 function calling (recipe)

Out of the box, Ollama's `gemma4:latest` ships a passthrough chat
template (`TEMPLATE {{ .Prompt }}`) — its built-in `RENDERER gemma4`
does not inject the tool list into the prompt or the
`<tool_call>...</tool_call>` markup the model expects. As a result a
plain `gemma4` model never emits tool calls in oxenClaw, even when
the prompt explicitly tells it to. Override with a Modelfile that
hands tools into the user turn and tells the model how to format
calls:

```
FROM gemma4:latest
TEMPLATE """{{- if or .System .Tools }}<start_of_turn>user
{{- if .System }}
{{ .System }}
{{- end }}
{{- if .Tools }}

You have access to the following tools. To call one, emit:
<tool_call>{"name": "<tool_name>", "arguments": {<arg_object>}}</tool_call>

After a <tool_response>...</tool_response> turn, use the result to
answer the user.

Available tools:
{{- range .Tools }}
{{ .Function }}
{{- end }}
{{- end }}<end_of_turn>
{{ end }}
{{- range $i, $msg := .Messages }}
...standard gemma turn cycle...
{{- end }}<start_of_turn>model
"""
PARAMETER stop <end_of_turn>
PARAMETER stop <start_of_turn>
```

Build with `ollama create gemma4-fc -f /path/to/Modelfile`.
Measured live: a 4-task tool-calling bench went from **0/16 tool
calls / 3/16 correct answers** on plain `gemma4` to **16/16 / 14/16**
on `gemma4-fc`.

The same bench also surfaced a separate streaming-parser bug —
gemma3/4 dump `tool_calls` in the FIRST streamed frame
(`done:false`) rather than in the final `done` frame. Fixed in
`oxenclaw/pi/providers/ollama.py` by hoisting the `tool_calls`
extraction out of the done branch so it runs per-frame. Without
that fix the Modelfile change alone is invisible to oxenClaw —
Ollama returns the tool calls but the parser drops them.
