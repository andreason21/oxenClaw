"""ModelRegistry + AuthStorage + provider id normalization.

Mirrors `@mariozechner/pi-coding-agent` `ModelRegistry` + `AuthStorage`.
The registry is a model id → `Model` map with alias and provider lookup;
the auth storage is a credential get/set keyed by `provider`. Both are
async to leave room for sqlite/secret-manager backends in later phases.

Inline provider (`pi-embedded-runner/model.inline-provider.ts`): runs
inside the same Python process for `local`, `proxy`, and `openai-
compatible` deployments. The registry exposes `inline_provider()` to
build the right `Api` for those models without going through a
credential lookup.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from dataclasses import replace
from typing import Any, Protocol

from oxenclaw.pi.models import Api, Model, ProviderId

# ─── Provider id normalization ────────────────────────────────────────


_PROVIDER_ALIASES: dict[str, ProviderId] = {
    "claude": "anthropic",
    "anthropic-claude": "anthropic",
    "vertex": "vertex-ai",
    "google-vertex": "vertex-ai",
    "gemini": "google",
    "openai-compat": "openai-compatible",
    "ollama-openai": "ollama",
    "lm-studio": "lmstudio",
    "llama-cpp": "llamacpp",
}


def normalize_provider_id(value: str) -> ProviderId:
    """Coerce common variants to canonical `ProviderId`. Unknown ids fall
    through unchanged so unfamiliar providers still route correctly when
    a wrapper has been registered for them."""
    canonical = _PROVIDER_ALIASES.get(value.lower(), value.lower())
    return canonical  # type: ignore[return-value]


# ─── AuthStorage ──────────────────────────────────────────────────────


class AuthStorage(Protocol):
    """Per-provider credential store."""

    async def get(self, provider: ProviderId) -> str | None: ...

    async def set(self, provider: ProviderId, api_key: str) -> None: ...

    async def delete(self, provider: ProviderId) -> bool: ...

    async def list_providers(self) -> list[ProviderId]: ...


class EnvAuthStorage:
    """Reads `<PROVIDER>_API_KEY` env vars; writes raise NotImplementedError.

    Practical default for dev — no on-disk credentials, no leakage. The
    sqlite-backed implementation lands in Phase 6 alongside the persistent
    SessionManager.
    """

    @staticmethod
    def _env_key(provider: ProviderId) -> str:
        return f"{provider.upper().replace('-', '_')}_API_KEY"

    async def get(self, provider: ProviderId) -> str | None:
        return os.environ.get(self._env_key(provider))

    async def set(self, provider: ProviderId, api_key: str) -> None:
        raise NotImplementedError(
            "EnvAuthStorage is read-only; use a persistent backend to store keys"
        )

    async def delete(self, provider: ProviderId) -> bool:
        raise NotImplementedError("EnvAuthStorage is read-only")

    async def list_providers(self) -> list[ProviderId]:
        out: list[ProviderId] = []
        for key in os.environ:
            if key.endswith("_API_KEY"):
                provider = key[: -len("_API_KEY")].lower().replace("_", "-")
                out.append(provider)  # type: ignore[arg-type]
        return out


class InMemoryAuthStorage:
    """Mutable AuthStorage for tests."""

    def __init__(self, initial: dict[ProviderId, str] | None = None) -> None:
        self._keys: dict[ProviderId, str] = dict(initial or {})

    async def get(self, provider: ProviderId) -> str | None:
        return self._keys.get(provider)

    async def set(self, provider: ProviderId, api_key: str) -> None:
        self._keys[provider] = api_key

    async def delete(self, provider: ProviderId) -> bool:
        return self._keys.pop(provider, None) is not None

    async def list_providers(self) -> list[ProviderId]:
        return list(self._keys)


class PoolBackedAuthStorage:
    """`AuthStorage` adapter that pulls keys from an `AuthKeyPool`.

    Falls back to `inner.get(...)` (e.g. `EnvAuthStorage`) when the
    pool has no entries for the requested provider. set/delete pass
    through to `inner` — the pool is read-mostly; rotation is the
    only mutation it owns.

    Together with `AuthKeyPool.report_failure`, this gives us the
    auth-rotation half of openclaw's auth-profile system without
    porting the full on-disk profile schema.
    """

    def __init__(self, pool: Any, inner: AuthStorage) -> None:
        self._pool = pool
        self._inner = inner
        # Track which (provider, key_id) the last `get(...)` returned
        # so callers can report back failure / success without having
        # to track it themselves.
        self._last: dict[ProviderId, str] = {}

    async def get(self, provider: ProviderId) -> str | None:
        out = await self._pool.get(provider)
        if out is not None:
            key_id, api_key = out
            self._last[provider] = key_id
            return api_key
        # Pool empty for this provider → fall through to env / inner.
        return await self._inner.get(provider)

    async def set(self, provider: ProviderId, api_key: str) -> None:
        await self._inner.set(provider, api_key)

    async def delete(self, provider: ProviderId) -> bool:
        return await self._inner.delete(provider)

    async def list_providers(self) -> list[ProviderId]:
        # Union of pool + inner — operators may have keys in both.
        seen = set(self._pool.by_provider.keys())
        for p in await self._inner.list_providers():
            seen.add(p)
        return list(seen)

    def last_key_id_for(self, provider: ProviderId) -> str | None:
        return self._last.get(provider)

    async def report_failure(
        self, provider: ProviderId, *, status: int | None = None
    ) -> None:
        key_id = self._last.get(provider)
        if key_id is not None:
            await self._pool.report_failure(provider, key_id, status=status)

    async def report_success(self, provider: ProviderId) -> None:
        key_id = self._last.get(provider)
        if key_id is not None:
            await self._pool.report_success(provider, key_id)


# ─── ModelRegistry ────────────────────────────────────────────────────


class ModelRegistry(Protocol):
    """Model id → Model map with alias and provider lookup."""

    def get(self, model_id: str) -> Model | None: ...

    def require(self, model_id: str) -> Model: ...

    def list(self) -> list[Model]: ...

    def by_provider(self, provider: ProviderId) -> list[Model]: ...

    def register(self, model: Model) -> None: ...


_PROVIDER_ID_GUESS: tuple[tuple[str, ProviderId], ...] = (
    ("claude", "anthropic"),
    ("anthropic-", "anthropic"),
    ("gpt-", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("gemini", "google"),
    ("deepseek", "deepseek"),
    ("qwen", "alibaba" if False else "openrouter"),  # qwen routed via OR
    ("mistral", "mistral"),
    ("llama", "ollama"),
    ("gemma", "ollama"),
)


def guess_provider_from_id(model_id: str) -> ProviderId:
    """Best-effort default provider from a model id prefix."""
    lower = model_id.lower()
    for prefix, provider in _PROVIDER_ID_GUESS:
        if lower.startswith(prefix):
            return provider  # type: ignore[return-value]
    return "openai-compatible"  # type: ignore[return-value]


class InMemoryModelRegistry:
    """Mutable registry. Provider wrappers can call `register(...)` at
    import time to advertise the models they support."""

    def __init__(self, models: Iterable[Model] = ()) -> None:
        self._by_id: dict[str, Model] = {}
        for m in models:
            self.register(m)

    def register(self, model: Model) -> None:
        canon = replace(model, provider=normalize_provider_id(model.provider))
        self._by_id[canon.id] = canon
        for alias in canon.aliases:
            # Aliases never overwrite an explicit registration.
            self._by_id.setdefault(alias, canon)

    def get(self, model_id: str) -> Model | None:
        return self._by_id.get(model_id)

    def require(self, model_id: str) -> Model:
        model = self.get(model_id)
        if model is None:
            raise KeyError(f"model {model_id!r} not registered")
        return model

    def list(self) -> list[Model]:
        # Deduplicate (aliases share Model instances).
        seen: set[str] = set()
        out: list[Model] = []
        for m in self._by_id.values():
            if m.id in seen:
                continue
            seen.add(m.id)
            out.append(m)
        return out

    def by_provider(self, provider: ProviderId) -> list[Model]:
        canon = normalize_provider_id(provider)
        return [m for m in self.list() if m.provider == canon]

    def __iter__(self) -> Iterator[Model]:
        return iter(self.list())

    def __len__(self) -> int:
        return len(self.list())


# ─── Remote (models.dev-backed) registry ──────────────────────────────


class RemoteModelRegistry(InMemoryModelRegistry):
    """`InMemoryModelRegistry` that lazily resolves unknown ids via
    `models.dev`.

    - The static catalog stays the fast path: `get(id)` / `require(id)`
      hit the in-memory dict before any network work.
    - On a miss, `require(id)` fetches the models.dev catalog (with its
      own four-level cache cascade), reads capabilities, builds a
      `Model`, registers it, and returns it.
    - When models.dev has no entry either, a `Model` is still produced
      using `CONTEXT_PROBE_TIERS[0]` as the context window so the runner
      has something to work with — the actual provider call will tell us
      if that was wrong.
    """

    def require(self, model_id: str) -> Model:
        existing = self.get(model_id)
        if existing is not None:
            return existing
        # Lazy import to avoid a hard dependency at import time and to
        # keep tests that monkey-patch `models_dev` honest.
        from oxenclaw.pi import models_dev as _mdev

        try:
            data = _mdev.fetch_models_dev()
        except Exception:  # noqa: BLE001
            data = {}
        caps = _mdev.get_model_capabilities(model_id, data) if data else {}

        provider_raw = caps.get("provider") if isinstance(caps, dict) else None
        if isinstance(provider_raw, str) and provider_raw:
            provider = normalize_provider_id(provider_raw)
        else:
            provider = guess_provider_from_id(model_id)

        ctx = caps.get("context_window") if isinstance(caps, dict) else None
        if not isinstance(ctx, int) or ctx <= 0:
            ctx = _mdev.CONTEXT_PROBE_TIERS[0]
        max_out = caps.get("max_output") if isinstance(caps, dict) else None
        if not isinstance(max_out, int) or max_out <= 0:
            max_out = 8192

        model = Model(
            id=model_id,
            provider=provider,
            context_window=ctx,
            max_output_tokens=max_out,
            supports_tools=bool(caps.get("supports_tools", True)) if caps else True,
            supports_image_input=bool(caps.get("supports_attachments", False)) if caps else False,
            supports_thinking=bool(caps.get("supports_reasoning", False)) if caps else False,
        )
        self.register(model)
        return model


# ─── Inline provider (local/proxy/openai-compatible) ──────────────────


# Default Api endpoints for the local-style providers, keyed by canonical
# provider id. `model.extra["base_url"]` overrides the default; this lets a
# single model id resolve against multiple deployments without registry
# duplication.
_INLINE_DEFAULT_BASE_URL: dict[ProviderId, str] = {
    "ollama": "http://127.0.0.1:11434/v1",
    "lmstudio": "http://127.0.0.1:1234/v1",
    "vllm": "http://127.0.0.1:8000/v1",
    "llamacpp": "http://127.0.0.1:8080/v1",
    "litellm": "http://127.0.0.1:4000/v1",
    "openai-compatible": "http://127.0.0.1:8000/v1",
    "proxy": "http://127.0.0.1:7332",
}


def inline_api(model: Model) -> Api:
    """Build an Api for a model that runs under an inline (local) provider.

    Looks up the base URL from `model.extra["base_url"]` first, then from
    the canonical default for the provider, then raises if neither exists.
    Inline models don't need credentials, but if `extra["api_key"]` is set
    we propagate it (some local proxies require a token).
    """
    base = model.extra.get("base_url") if isinstance(model.extra.get("base_url"), str) else None
    if base is None:
        base = _INLINE_DEFAULT_BASE_URL.get(model.provider)
    if base is None:
        raise ValueError(
            f"no inline base_url for model {model.id!r} (provider={model.provider!r}); "
            f"set model.extra['base_url'] or use a hosted provider"
        )
    api_key = model.extra.get("api_key") if isinstance(model.extra.get("api_key"), str) else None
    return Api(base_url=base, api_key=api_key)


def is_inline_provider(provider: ProviderId) -> bool:
    """True if this provider runs under a local/inline endpoint that needs
    no external auth flow."""
    return provider in _INLINE_DEFAULT_BASE_URL


__all__ = [
    "AuthStorage",
    "EnvAuthStorage",
    "InMemoryAuthStorage",
    "InMemoryModelRegistry",
    "ModelRegistry",
    "RemoteModelRegistry",
    "guess_provider_from_id",
    "inline_api",
    "is_inline_provider",
    "normalize_provider_id",
]
