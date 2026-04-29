"""Local `llama-server` lifecycle manager.

oxenclaw normally talks to externally-managed inference services (Ollama,
vLLM, an already-running llama.cpp server). This package adds the
opposite path: oxenclaw spawns and owns its own `llama-server` process,
the way unsloth-studio does, so the same GGUF runs ~1.3-2x faster than
through Ollama (no Modelfile re-encoding, `--flash-attn on` forced,
`--jinja`, full-GPU offload by default, persistent warm KV).

Public surface:

- `LlamaCppServer`         — process supervisor (singleton via
                             `get_default_server()`).
- `LlamaCppServerError`    — raised on spawn / health-check failure.
- `get_default_server()`   — process-global singleton accessor.

The provider stream wrapper at
`oxenclaw.pi.providers.llamacpp_direct` consumes this manager.
"""

from oxenclaw.pi.llamacpp_server.manager import (
    LlamaCppServer,
    LlamaCppServerError,
    get_default_server,
    get_embedding_server,
)

__all__ = [
    "LlamaCppServer",
    "LlamaCppServerError",
    "get_default_server",
    "get_embedding_server",
]
