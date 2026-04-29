"""Provider stream wrappers.

oxenClaw ships with **five** provider wrappers, all targeting on-host /
LAN inference. Each module registers itself via
`register_provider_stream(provider_id, fn)` at import time.

Bundled providers:

- `ollama`            — native `/api/chat`, bypasses Ollama's OpenAI
                        shim because the shim drops `num_ctx` and
                        tool-call deltas
- `llamacpp-direct`   — oxenClaw spawns and owns its own `llama-server`
                        with the unsloth-studio fast preset
                        (`--flash-attn on --jinja --no-context-shift`,
                        full GPU offload). ~3x faster decode than
                        Ollama on the same GGUF
- `llamacpp`          — externally-managed llama.cpp server (you start
                        `llama-server` yourself, oxenClaw just talks to
                        it via OpenAI-compat)
- `vllm`              — externally-managed vLLM server (OpenAI-compat)
- `lmstudio`          — externally-managed LM Studio server
                        (OpenAI-compat)

The `--provider auto` default at the CLI picks `llamacpp-direct` when
configured, else `ollama`. See `docs/AGENTS.md` for the full agent
configuration guide.
"""

from oxenclaw.pi.providers import (  # noqa: F401  (registers via side effect)
    llamacpp_direct,
    ollama,
    openai,
)
