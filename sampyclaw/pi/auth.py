"""High-level credential resolution.

Combines `Model` + `AuthStorage` → `Api`. For inline (local) providers,
delegates to `inline_api`; for hosted providers, looks up the API key
from `AuthStorage` and synthesises the canonical base URL.
"""

from __future__ import annotations

from sampyclaw.pi.models import Api, Model, ProviderId
from sampyclaw.pi.registry import AuthStorage, inline_api, is_inline_provider

# Canonical base URLs for hosted providers we know about. Adapters are free
# to override via `model.extra["base_url"]`.
_HOSTED_DEFAULT_BASE_URL: dict[ProviderId, str] = {
    "anthropic": "https://api.anthropic.com",
    "anthropic-vertex": "https://us-central1-aiplatform.googleapis.com",
    "openai": "https://api.openai.com/v1",
    "google": "https://generativelanguage.googleapis.com",
    "vertex-ai": "https://us-central1-aiplatform.googleapis.com",
    "bedrock": "https://bedrock-runtime.us-east-1.amazonaws.com",
    "openrouter": "https://openrouter.ai/api/v1",
    "moonshot": "https://api.moonshot.ai/v1",
    "minimax": "https://api.minimaxi.chat/v1",
    "zai": "https://open.bigmodel.cn/api/paas/v4",
    "kilocode": "https://kilocode.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "mistral": "https://api.mistral.ai/v1",
    "together": "https://api.together.xyz/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
}


class MissingCredential(RuntimeError):
    """Raised when a hosted provider has no API key in storage."""


async def resolve_api(model: Model, auth: AuthStorage) -> Api:
    """Build an `Api` for `model`, fetching credentials as needed.

    Inline providers (Ollama / LM Studio / vLLM / llama.cpp / litellm /
    proxy / openai-compatible) don't need credentials and synthesise the
    base URL from `model.extra["base_url"]` or the inline default.

    Hosted providers require an API key; if `auth.get(provider)` returns
    None we raise `MissingCredential` rather than silently calling the
    endpoint and getting a 401 — surfacing the misconfig at the boundary
    is much easier to diagnose.
    """
    if is_inline_provider(model.provider):
        return inline_api(model)

    api_key = await auth.get(model.provider)
    if api_key is None:
        raise MissingCredential(
            f"no credential for provider {model.provider!r}; "
            f"set the matching env var or store via AuthStorage"
        )
    base = (
        model.extra.get("base_url")
        if isinstance(model.extra.get("base_url"), str)
        else _HOSTED_DEFAULT_BASE_URL.get(model.provider)
    )
    if base is None:
        raise ValueError(
            f"no default base URL for hosted provider {model.provider!r}; "
            f"set model.extra['base_url']"
        )
    return Api(base_url=base, api_key=api_key)


__all__ = ["MissingCredential", "resolve_api"]
