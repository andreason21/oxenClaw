"""Async embedding providers — pluggable registry for vector embeddings.

Default provider is an OpenAI-compatible endpoint (Ollama by default).
Additional providers: Anthropic/Voyage and Cohere, each accessed via httpx.

Factory
-------
Use `build_embedder(provider, model=None, **kwargs)` to get an embedder by
name.  Supported providers:

  * ``"ollama"``            → OpenAIEmbeddings at 127.0.0.1:11434/v1
  * ``"llamacpp-direct"``   → managed `llama-server --embedding` spawned
                              by oxenClaw (no Ollama required)
  * ``"openai-compatible"`` → OpenAIEmbeddings (caller supplies base_url)
  * ``"openai"``            → OpenAIEmbeddings at api.openai.com/v1
  * ``"anthropic"``         → AnthropicEmbeddings (Voyage AI, ANTHROPIC_API_KEY)
  * ``"voyage"``            → AnthropicEmbeddings (alias; VOYAGE_API_KEY / ANTHROPIC_API_KEY)
  * ``"cohere"``            → CohereEmbeddings (COHERE_API_KEY)

TODO — providers pending a future session
-----------------------------------------
* ``"bedrock"``      — AWS Bedrock Titan/Cohere embeddings via boto3
* ``"google"``       — Google text-embedding-004 via google-generativeai
* ``"mistral"``      — Mistral embed via mistral SDK
* ``"together"``     — together.ai /v1/embeddings (OpenAI-compat shape)
* ``"fireworks"``    — fireworks.ai /v1/embeddings (OpenAI-compat shape)
* ``"azure"``        — Azure OpenAI /openai/deployments/…/embeddings
* ``"jina"``         — Jina AI embeddings v3 via their REST API
"""

from __future__ import annotations

import os
from typing import Protocol, runtime_checkable

import aiohttp
import httpx

from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("memory.embeddings")

DEFAULT_EMBED_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_EMBED_MODEL = "nomic-embed-text"
DEFAULT_TIMEOUT = 60.0

# Voyage AI (Anthropic's embedding partner)
DEFAULT_VOYAGE_BASE_URL = "https://api.voyageai.com/v1"
DEFAULT_VOYAGE_MODEL = "voyage-3"

# Cohere
DEFAULT_COHERE_BASE_URL = "https://api.cohere.com/v2"
DEFAULT_COHERE_MODEL = "embed-english-v3.0"


class EmbeddingError(RuntimeError):
    """Network / protocol failure talking to the embedding endpoint."""


class UnknownEmbedderProvider(ValueError):
    """Raised by `build_embedder` for unrecognised provider names."""


# ---------------------------------------------------------------------------
# Legacy synchronous-property Protocol (kept for backward compatibility with
# EmbeddingCache and existing code that references EmbeddingProvider).
# ---------------------------------------------------------------------------


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Anything that turns texts into fixed-dimension float vectors.

    .. deprecated::
        Prefer the new `Embedder` Protocol which uses async ``dim()`` /
        ``embed_batch()`` instead of synchronous properties.  This Protocol
        is kept so existing code (EmbeddingCache, tests) does not break.
    """

    @property
    def dimensions(self) -> int: ...

    @property
    def model(self) -> str: ...

    @property
    def provider_name(self) -> str: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    async def aclose(self) -> None: ...


# ---------------------------------------------------------------------------
# New typed Protocol surface — used by the factory and new providers.
# ---------------------------------------------------------------------------


@runtime_checkable
class Embedder(Protocol):
    """Pluggable async embedding provider.

    All concrete classes below implement this Protocol.  The ``provider``
    and ``model`` attributes are plain string instance attributes so that
    ``isinstance(obj, Embedder)`` returns ``True`` without calling any
    methods.
    """

    provider: str  # e.g. "openai-compatible", "anthropic", "cohere"
    model: str

    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...

    async def dim(self) -> int: ...

    async def aclose(self) -> None: ...


# ---------------------------------------------------------------------------
# OpenAI-compatible provider (Ollama / LM Studio / vLLM / OpenAI)
# ---------------------------------------------------------------------------


class OpenAIEmbeddings:
    """OpenAI-compatible /v1/embeddings client.

    Implements both ``EmbeddingProvider`` (legacy) and ``Embedder`` (new).
    Dimensions are discovered on the first call (lazy) so callers don't
    have to hard-code a value when swapping models.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_EMBED_BASE_URL,
        model: str = DEFAULT_EMBED_MODEL,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout = timeout
        self._http: aiohttp.ClientSession | None = http_session
        self._owns_session = http_session is None
        self._dimensions: int | None = None
        # Embedder Protocol attrs
        self.provider = "openai-compatible"
        self.model = model

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def provider_name(self) -> str:
        return "openai-compat"

    @property
    def dimensions(self) -> int:
        # Returns 0 until the first embed() probes the live model. We
        # used to raise here, but `runtime_checkable` Protocol isinstance
        # checks on Python 3.11 call `hasattr` on every protocol attr —
        # for a property that means evaluating the getter, and a
        # RuntimeError propagates out of the isinstance() call. Callers
        # that need the real dimensions should `await dim()` (which
        # probes if needed) or treat 0 as "unknown".
        return self._dimensions or 0

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._http is None:
            self._http = aiohttp.ClientSession()
            self._owns_session = True
        return self._http

    async def aclose(self) -> None:
        if self._owns_session and self._http is not None:
            await self._http.close()
            self._http = None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        session = await self._ensure_session()
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        url = f"{self._base_url}/embeddings"
        try:
            async with session.post(
                url,
                json={"model": self._model, "input": texts},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    hint = ""
                    if resp.status == 404:
                        hint = (
                            f" — endpoint or model not found. If you're using "
                            f"Ollama, run `ollama pull {self._model}` on the "
                            f"host serving {self._base_url}. Verify the URL "
                            f"with `curl {self._base_url}/embeddings -d "
                            f'\'{{"model":"{self._model}","input":"hi"}}\'`.'
                        )
                    raise EmbeddingError(
                        f"embeddings endpoint returned {resp.status}: {body[:300]}{hint}"
                    )
                data = await resp.json()
        except aiohttp.ClientError as exc:
            raise EmbeddingError(str(exc)) from exc

        out: list[list[float]] = []
        for entry in data.get("data") or []:
            vec = entry.get("embedding")
            if not isinstance(vec, list):
                raise EmbeddingError("embedding response missing `embedding` field")
            out.append([float(x) for x in vec])
        if not out:
            raise EmbeddingError("embedding response had zero vectors")
        if self._dimensions is None:
            self._dimensions = len(out[0])
        return out

    # ------------------------------------------------------------------
    # Embedder Protocol surface (async dim + embed_batch alias)
    # ------------------------------------------------------------------

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Alias for `embed` — satisfies the `Embedder` Protocol."""
        return await self.embed(texts)

    async def dim(self) -> int:
        """Return embedding dimensions, fetching a probe vector if needed."""
        if self._dimensions is None:
            await self.embed(["probe"])
        return self.dimensions


# ---------------------------------------------------------------------------
# Anthropic / Voyage AI embeddings
# ---------------------------------------------------------------------------


class AnthropicEmbeddings:
    """Voyage AI embedding client (Anthropic's embedding partner).

    The Voyage AI REST API accepts the same ``/v1/embeddings`` path with a
    JSON body ``{"model": "…", "input": ["text1", …]}``.  Auth is via the
    ``Authorization: Bearer <key>`` header.

    API key resolution order:
    1. ``api_key`` kwarg
    2. ``ANTHROPIC_API_KEY`` env var
    3. ``VOYAGE_API_KEY`` env var
    """

    provider = "anthropic"

    def __init__(
        self,
        *,
        model: str = DEFAULT_VOYAGE_MODEL,
        api_key: str | None = None,
        base_url: str = DEFAULT_VOYAGE_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._api_key = (
            api_key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("VOYAGE_API_KEY") or ""
        )
        self._client: httpx.AsyncClient | None = None
        self._dimensions: int | None = None

    @property
    def provider_name(self) -> str:
        return self.provider

    @property
    def dimensions(self) -> int:
        # See OpenAIEmbeddings.dimensions for why we return 0 instead of
        # raising when not yet probed.
        return self._dimensions or 0

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        url = f"{self._base_url}/embeddings"
        try:
            resp = await self._get_client().post(
                url,
                json={"model": self.model, "input": texts},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise EmbeddingError(str(exc)) from exc
        if resp.status_code >= 400:
            raise EmbeddingError(
                f"Voyage embeddings endpoint returned {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()
        out: list[list[float]] = []
        for entry in data.get("data") or []:
            vec = entry.get("embedding")
            if not isinstance(vec, list):
                raise EmbeddingError("Voyage response missing `embedding` field")
            out.append([float(x) for x in vec])
        if not out:
            raise EmbeddingError("Voyage response had zero vectors")
        if self._dimensions is None:
            self._dimensions = len(out[0])
        return out

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return await self.embed(texts)

    async def dim(self) -> int:
        if self._dimensions is None:
            await self.embed(["probe"])
        return self.dimensions


# ---------------------------------------------------------------------------
# Cohere embeddings
# ---------------------------------------------------------------------------


class CohereEmbeddings:
    """Cohere /v2/embed client.

    Uses httpx directly (no cohere SDK dependency).  The v2 API returns
    embeddings as ``response["embeddings"]["float"]``.

    API key resolution order:
    1. ``api_key`` kwarg
    2. ``COHERE_API_KEY`` env var

    ``input_type`` defaults to ``"search_document"`` for indexing.  Pass
    ``input_type="search_query"`` when embedding a query.
    """

    provider = "cohere"

    def __init__(
        self,
        *,
        model: str = DEFAULT_COHERE_MODEL,
        api_key: str | None = None,
        base_url: str = DEFAULT_COHERE_BASE_URL,
        input_type: str = "search_document",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._input_type = input_type
        self._timeout = timeout
        self._api_key = api_key or os.environ.get("COHERE_API_KEY") or ""
        self._client: httpx.AsyncClient | None = None
        self._dimensions: int | None = None

    @property
    def provider_name(self) -> str:
        return self.provider

    @property
    def dimensions(self) -> int:
        # See OpenAIEmbeddings.dimensions for why we return 0 instead of
        # raising when not yet probed.
        return self._dimensions or 0

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        url = f"{self._base_url}/embed"
        try:
            resp = await self._get_client().post(
                url,
                json={
                    "model": self.model,
                    "texts": texts,
                    "input_type": self._input_type,
                    "embedding_types": ["float"],
                },
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise EmbeddingError(str(exc)) from exc
        if resp.status_code >= 400:
            raise EmbeddingError(
                f"Cohere embed endpoint returned {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()
        # v2 shape: {"embeddings": {"float": [[...], ...]}}
        embeddings_block = data.get("embeddings") or {}
        float_vecs = embeddings_block.get("float") or []
        if not float_vecs:
            raise EmbeddingError("Cohere response had zero float vectors")
        out = [[float(x) for x in vec] for vec in float_vecs]
        if self._dimensions is None:
            self._dimensions = len(out[0])
        return out

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return await self.embed(texts)

    async def dim(self) -> int:
        if self._dimensions is None:
            await self.embed(["probe"])
        return self.dimensions


# ---------------------------------------------------------------------------
# llamacpp-direct embedder — wraps OpenAIEmbeddings with a managed-server
# bootstrap so the user doesn't have to run `llama-server --embedding`
# themselves.
# ---------------------------------------------------------------------------


class LlamaCppDirectEmbeddings:
    """OpenAI-compatible embedder backed by a `llama-server --embedding`
    child process that oxenClaw spawns and owns.

    Required:
        gguf_path — absolute path to an embedding GGUF (e.g.
        `nomic-embed-text-v2-moe.Q4_K_M.gguf`).

    Optional:
        n_ctx, n_gpu_layers, n_threads, n_parallel, pooling — forwarded
        to `LlamaCppServerSpec`.

    On the first call to `embed`/`embed_batch`/`dim`, the manager spawns
    `llama-server --embedding` on a free localhost port, waits for
    `/health`, then this class delegates to a sync-resolved
    `OpenAIEmbeddings` against that port.
    """

    def __init__(
        self,
        *,
        gguf_path: str,
        model: str = "embed-local",
        n_ctx: int = 8192,
        n_gpu_layers: int = 999,
        n_threads: int = -1,
        n_parallel: int = 4,
        # `mean` is the right default for the embedding GGUFs people
        # actually use here (nomic-embed-text-v2-moe, bge-*, e5-*).
        # Leaving pooling unset makes llama-server emit per-token
        # vectors that look healthy on the wire but score 0.0 against
        # everything because they're not pooled to a single vector.
        # Override with $OXENCLAW_LLAMACPP_EMBED_POOLING for models
        # that need `cls` / `last`.
        pooling: str | None = "mean",
        timeout: float = DEFAULT_TIMEOUT,
        api_key: str | None = None,
        health_timeout_s: float = 600.0,
    ) -> None:
        self._gguf_path = gguf_path
        self._model = model
        self._n_ctx = n_ctx
        self._n_gpu_layers = n_gpu_layers
        self._n_threads = n_threads
        self._n_parallel = n_parallel
        self._pooling = pooling
        self._timeout = timeout
        self._api_key = api_key
        self._health_timeout_s = health_timeout_s
        self._inner: OpenAIEmbeddings | None = None
        self._lock = __import__("threading").Lock()

    @property
    def provider_name(self) -> str:
        return "llamacpp-direct"

    @property
    def dimensions(self) -> int:
        if self._inner is None:
            return 0
        return self._inner.dimensions

    @property
    def model(self) -> str:
        return self._model

    def _ensure_inner(self) -> OpenAIEmbeddings:
        with self._lock:
            if self._inner is not None:
                return self._inner
            from pathlib import Path as _Path

            from oxenclaw.pi.llamacpp_server import (
                LlamaCppServerError,
                get_embedding_server,
            )
            from oxenclaw.pi.llamacpp_server.manager import LlamaCppServerSpec

            gguf = _Path(os.path.expanduser(self._gguf_path))
            if not gguf.is_file():
                raise EmbeddingError(
                    f"llamacpp-direct embedder: GGUF not found at {gguf}. "
                    f"Set OXENCLAW_LLAMACPP_EMBED_GGUF or pass gguf_path=..."
                )
            spec = LlamaCppServerSpec(
                gguf_path=gguf,
                n_ctx=self._n_ctx,
                n_gpu_layers=self._n_gpu_layers,
                n_threads=self._n_threads,
                n_parallel=self._n_parallel,
                embedding=True,
                pooling=self._pooling,
                # Chat-only flags are dropped automatically when embedding=True.
            )
            try:
                base_url = get_embedding_server().ensure_loaded(
                    spec, health_timeout_s=self._health_timeout_s
                )
            except LlamaCppServerError as exc:
                raise EmbeddingError(f"llamacpp-direct embed-server boot failed: {exc}") from exc
            self._inner = OpenAIEmbeddings(
                base_url=base_url,
                model=self._model,
                api_key=self._api_key,
                timeout=self._timeout,
            )
            return self._inner

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await self._ensure_inner().embed(texts)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return await self._ensure_inner().embed_batch(texts)

    async def dim(self) -> int:
        return await self._ensure_inner().dim()

    async def aclose(self) -> None:
        if self._inner is not None:
            await self._inner.aclose()
            self._inner = None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_OPENAI_COMPAT_PROVIDERS = {"ollama", "openai-compatible", "openai", "vllm", "lmstudio"}

# Default embedding GGUF for the managed path. Picked because it's
# multilingual, MoE-quantised down to ~370 MiB at Q4, and works with
# the OpenAI-compat /v1/embeddings shape llama-server exposes.
DEFAULT_LLAMACPP_EMBED_MODEL = "embed-local"

_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "ollama": {"base_url": DEFAULT_EMBED_BASE_URL, "model": DEFAULT_EMBED_MODEL},
    "llamacpp-direct": {"model": DEFAULT_LLAMACPP_EMBED_MODEL},
    "openai-compatible": {"base_url": DEFAULT_EMBED_BASE_URL, "model": DEFAULT_EMBED_MODEL},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "text-embedding-3-small"},
    "vllm": {"base_url": "http://127.0.0.1:8000/v1", "model": DEFAULT_EMBED_MODEL},
    "lmstudio": {"base_url": "http://127.0.0.1:1234/v1", "model": DEFAULT_EMBED_MODEL},
    "anthropic": {"model": DEFAULT_VOYAGE_MODEL},
    "voyage": {"model": DEFAULT_VOYAGE_MODEL},
    "cohere": {"model": DEFAULT_COHERE_MODEL},
}


def build_embedder(
    provider: str,
    model: str | None = None,
    **kwargs: object,
) -> Embedder:
    """Return a configured ``Embedder`` for the given provider name.

    Parameters
    ----------
    provider:
        One of the supported provider names (see module docstring).
    model:
        Override the default model for this provider.  When ``None`` the
        provider's default is used.
    **kwargs:
        Passed directly to the embedder constructor (e.g. ``base_url``,
        ``api_key``, ``timeout``).

    Raises
    ------
    UnknownEmbedderProvider
        If *provider* is not recognised.
    """
    if provider not in _PROVIDER_DEFAULTS:
        known = ", ".join(sorted(_PROVIDER_DEFAULTS))
        raise UnknownEmbedderProvider(
            f"unknown embedder provider {provider!r}. "
            f"Supported: {known}. "
            f"See oxenclaw/memory/embeddings.py module docstring for the TODO list."
        )

    defaults = dict(_PROVIDER_DEFAULTS[provider])
    if model is not None:
        defaults["model"] = model
    # kwargs override defaults (caller wins)
    merged: dict[str, object] = {**defaults, **kwargs}

    if provider == "llamacpp-direct":
        # GGUF path resolution: explicit kwarg → env var → error.
        gguf = merged.pop("gguf_path", None) or os.environ.get(
            "OXENCLAW_LLAMACPP_EMBED_GGUF", ""
        ).strip()
        if not gguf:
            raise UnknownEmbedderProvider(
                "llamacpp-direct embedder: no GGUF configured. Set "
                "OXENCLAW_LLAMACPP_EMBED_GGUF=/path/to/embed.gguf or pass "
                "gguf_path=... to build_embedder()."
            )
        # Optional knobs from env when caller didn't override.
        merged.setdefault(
            "n_ctx",
            int(os.environ.get("OXENCLAW_LLAMACPP_EMBED_CTX") or 8192),
        )
        merged.setdefault(
            "n_gpu_layers",
            int(os.environ.get("OXENCLAW_LLAMACPP_EMBED_NGL") or 999),
        )
        # Only set pooling when the env var explicitly chooses one;
        # otherwise let the LlamaCppDirectEmbeddings constructor's own
        # default (`mean`) take effect. Forwarding `None` here would
        # clobber that and silently produce zero-similarity vectors.
        env_pooling = os.environ.get("OXENCLAW_LLAMACPP_EMBED_POOLING", "").strip()
        if env_pooling:
            merged["pooling"] = env_pooling
        # `base_url` is meaningless for managed servers — strip it out
        # before forwarding.
        merged.pop("base_url", None)
        return LlamaCppDirectEmbeddings(gguf_path=str(gguf), **merged)  # type: ignore[arg-type]

    if provider in _OPENAI_COMPAT_PROVIDERS:
        # Auto-resolve API key from env for OpenAI
        if provider == "openai" and "api_key" not in merged:
            env_key = os.environ.get("OPENAI_API_KEY")
            if env_key:
                merged["api_key"] = env_key
        return OpenAIEmbeddings(**merged)  # type: ignore[arg-type]

    if provider in ("anthropic", "voyage"):
        # _PROVIDER_DEFAULTS has no base_url for these; the class default
        # (api.voyageai.com) is used unless the caller explicitly supplied one.
        return AnthropicEmbeddings(**merged)  # type: ignore[arg-type]

    if provider == "cohere":
        return CohereEmbeddings(**merged)  # type: ignore[arg-type]

    # Unreachable — _PROVIDER_DEFAULTS guard above catches unknowns.
    raise UnknownEmbedderProvider(provider)  # pragma: no cover
