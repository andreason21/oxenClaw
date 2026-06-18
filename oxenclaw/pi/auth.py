"""High-level credential resolution.

Combines `Model` + `AuthStorage` → `Api`. Local-first providers (Ollama /
LM Studio / vLLM / llama.cpp / llamacpp-direct) are `is_inline_provider(...)`
→ True and resolve via `inline_api(model)` with no credential. The opt-in
hosted providers (`openai`, `gemini`, `azure-openai`) take the credentialed
path: an API key from `AuthStorage` plus a base URL from
`model.extra['base_url']` or `_HOSTED_DEFAULT_BASE_URL`. External plugins can
register further hosted providers by extending `_HOSTED_DEFAULT_BASE_URL`.
"""

from __future__ import annotations

from oxenclaw.pi.models import Api, Model, ProviderId
from oxenclaw.pi.registry import AuthStorage, inline_api, is_inline_provider

# Default base URLs for the bundled hosted providers. oxenClaw is local-first;
# these only matter when an agent is explicitly configured with one of these
# provider ids (and the matching `<PROVIDER>_API_KEY`). `azure-openai` is
# intentionally absent — an Azure endpoint is resource-specific, so the
# operator must supply `model.extra['base_url']` / `--base-url`. Plugins can
# append further hosted providers to this dict at import time.
_HOSTED_DEFAULT_BASE_URL: dict[ProviderId, str] = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}


class MissingCredential(RuntimeError):
    """Raised when a hosted provider has no API key in storage."""


async def resolve_api(model: Model, auth: AuthStorage) -> Api:
    """Build an `Api` for `model`, fetching credentials as needed.

    Inline providers (Ollama / LM Studio / vLLM / llama.cpp /
    llamacpp-direct) don't need credentials and synthesise the base
    URL from `model.extra["base_url"]` or the inline default.

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
