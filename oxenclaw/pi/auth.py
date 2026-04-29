"""High-level credential resolution.

Combines `Model` + `AuthStorage` → `Api`. The catalog is on-host-only,
so every supported provider is `is_inline_provider(...)` → True and the
resolution is just a thin wrapper around `inline_api(model)`. The
hosted-provider code path is preserved as a stub so external plugins
that register their own provider id can still slot in by extending
`_HOSTED_DEFAULT_BASE_URL` (currently empty).
"""

from __future__ import annotations

from oxenclaw.pi.models import Api, Model, ProviderId
from oxenclaw.pi.registry import AuthStorage, inline_api, is_inline_provider

# Empty by default: oxenClaw's bundled catalog is local-only. Plugins that
# want to add a hosted provider should append to this dict at import time.
_HOSTED_DEFAULT_BASE_URL: dict[ProviderId, str] = {}


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
