"""Provider stream wrappers.

Each module in this subpackage is responsible for one provider family:
translating its native SSE / response shape into the pi `AssistantMessageEvent`
union and registering itself via `register_provider_stream(provider_id, fn)`
at import time.

Importing `oxenclaw.pi.providers` registers all bundled wrappers. Operators
that don't want a wrapper loaded can import individual modules selectively.

Bundled providers:
- `openai`         (also covers ollama / lmstudio / vllm / llamacpp / litellm
                    / openai-compatible / proxy via the same OpenAI-style
                    chat-completions SSE shape)
- `anthropic`      (Anthropic native SSE with cache_control + thinking)
- `google`         (Gemini generateContent SSE with thinking config)
- `bedrock`        (AWS Bedrock invoke-model — wraps anthropic family
                    payload semantics)
- `openrouter`     (capability-aware OpenAI-shape; thin wrapper)
- `moonshot`       (OpenAI-compat with optional thinking-stream variant)
- `zai`            (OpenAI-compat with payload patch)
- `minimax`        (OpenAI-compat with extra params)
"""

from oxenclaw.pi.providers import (  # noqa: F401  (registers via side effect)
    anthropic,
    bedrock,
    google,
    minimax,
    moonshot,
    openai,
    openrouter,
    zai,
)
