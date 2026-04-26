"""Async embedding provider — defaults to a local Ollama embedding model.

Uses the OpenAI `/v1/embeddings` shape, which Ollama / LM Studio / vLLM
all expose. Pluggable Protocol so callers can supply their own (e.g. for
tests or commercial endpoints).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import aiohttp

from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("memory.embeddings")

DEFAULT_EMBED_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_EMBED_MODEL = "nomic-embed-text"
DEFAULT_TIMEOUT = 60.0


class EmbeddingError(RuntimeError):
    """Network / protocol failure talking to the embedding endpoint."""


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Anything that turns texts into fixed-dimension float vectors."""

    @property
    def dimensions(self) -> int: ...

    @property
    def model(self) -> str: ...

    @property
    def provider_name(self) -> str: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...

    async def aclose(self) -> None: ...


class OpenAIEmbeddings:
    """OpenAI-compatible /v1/embeddings client.

    Implements `EmbeddingProvider`. Dimensions are discovered on the first
    call (lazy) so callers don't have to hard-code a value when swapping
    models.
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

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def provider_name(self) -> str:
        return "openai-compat"

    @property
    def dimensions(self) -> int:
        if self._dimensions is None:
            raise RuntimeError("embedding dimensions not yet known — call embed() at least once")
        return self._dimensions

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
                            f"'{{\"model\":\"{self._model}\",\"input\":\"hi\"}}'`."
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
