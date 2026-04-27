"""Tests for the pluggable embedder registry and factory.

Covers:
* build_embedder factory routing
* AnthropicEmbeddings shape (via respx mocking httpx)
* CohereEmbeddings shape (via respx mocking httpx)
* Protocol conformance for all three concrete providers
"""

from __future__ import annotations

import pytest
import respx
import httpx

from oxenclaw.memory.embeddings import (
    AnthropicEmbeddings,
    CohereEmbeddings,
    Embedder,
    EmbeddingProvider,
    OpenAIEmbeddings,
    UnknownEmbedderProvider,
    build_embedder,
)


# ---------------------------------------------------------------------------
# Factory routing tests
# ---------------------------------------------------------------------------


def test_factory_returns_openai_for_ollama() -> None:
    embedder = build_embedder("ollama")
    assert isinstance(embedder, OpenAIEmbeddings)
    assert embedder.base_url == "http://127.0.0.1:11434/v1"
    assert embedder.model == "nomic-embed-text"


def test_factory_returns_openai_for_openai_provider() -> None:
    embedder = build_embedder("openai", model="text-embedding-3-large")
    assert isinstance(embedder, OpenAIEmbeddings)
    assert embedder.base_url == "https://api.openai.com/v1"
    assert embedder.model == "text-embedding-3-large"


def test_factory_returns_anthropic_when_requested() -> None:
    embedder = build_embedder("anthropic")
    assert isinstance(embedder, AnthropicEmbeddings)
    assert embedder.provider == "anthropic"
    assert embedder.model == "voyage-3"


def test_factory_returns_voyage_alias() -> None:
    embedder = build_embedder("voyage", model="voyage-3-lite")
    assert isinstance(embedder, AnthropicEmbeddings)
    assert embedder.model == "voyage-3-lite"


def test_factory_returns_cohere_when_requested() -> None:
    embedder = build_embedder("cohere")
    assert isinstance(embedder, CohereEmbeddings)
    assert embedder.provider == "cohere"
    assert embedder.model == "embed-english-v3.0"


def test_factory_raises_for_unknown_provider() -> None:
    with pytest.raises(UnknownEmbedderProvider, match="unknown embedder provider"):
        build_embedder("totally-made-up-provider")


def test_factory_model_override() -> None:
    embedder = build_embedder("cohere", model="embed-multilingual-v3.0")
    assert isinstance(embedder, CohereEmbeddings)
    assert embedder.model == "embed-multilingual-v3.0"


def test_factory_kwargs_forwarded_to_openai() -> None:
    embedder = build_embedder("openai-compatible", base_url="http://my-vllm:8080/v1", model="my-model")
    assert isinstance(embedder, OpenAIEmbeddings)
    assert embedder.base_url == "http://my-vllm:8080/v1"
    assert embedder.model == "my-model"


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_openai_embeddings_conforms_to_embedder_protocol() -> None:
    embedder = OpenAIEmbeddings()
    assert isinstance(embedder, Embedder)


def test_openai_embeddings_conforms_to_legacy_provider_protocol() -> None:
    embedder = OpenAIEmbeddings()
    assert isinstance(embedder, EmbeddingProvider)


def test_anthropic_embeddings_conforms_to_embedder_protocol() -> None:
    embedder = AnthropicEmbeddings()
    assert isinstance(embedder, Embedder)


def test_cohere_embeddings_conforms_to_embedder_protocol() -> None:
    embedder = CohereEmbeddings()
    assert isinstance(embedder, Embedder)


# ---------------------------------------------------------------------------
# AnthropicEmbeddings HTTP shape (respx mocking httpx)
# ---------------------------------------------------------------------------

_VOYAGE_RESPONSE = {
    "object": "list",
    "data": [
        {"object": "embedding", "embedding": [0.1, 0.2, 0.3, 0.4], "index": 0},
        {"object": "embedding", "embedding": [0.5, 0.6, 0.7, 0.8], "index": 1},
    ],
    "model": "voyage-3",
    "usage": {"total_tokens": 8},
}


@respx.mock
async def test_anthropic_embeddings_shape() -> None:
    """AnthropicEmbeddings posts correct URL/body and parses the response."""
    route = respx.post("https://api.voyageai.com/v1/embeddings").mock(
        return_value=httpx.Response(200, json=_VOYAGE_RESPONSE)
    )

    embedder = AnthropicEmbeddings(api_key="test-voyage-key", model="voyage-3")
    try:
        result = await embedder.embed(["hello world", "foo bar"])
    finally:
        await embedder.aclose()

    assert route.called
    request = route.calls.last.request
    import json
    body = json.loads(request.content)
    assert body["model"] == "voyage-3"
    assert body["input"] == ["hello world", "foo bar"]
    assert request.headers["authorization"] == "Bearer test-voyage-key"

    assert len(result) == 2
    assert result[0] == pytest.approx([0.1, 0.2, 0.3, 0.4])
    assert result[1] == pytest.approx([0.5, 0.6, 0.7, 0.8])


@respx.mock
async def test_anthropic_embeddings_embed_batch_alias() -> None:
    respx.post("https://api.voyageai.com/v1/embeddings").mock(
        return_value=httpx.Response(200, json=_VOYAGE_RESPONSE)
    )
    embedder = AnthropicEmbeddings(api_key="k", model="voyage-3")
    try:
        result = await embedder.embed_batch(["hello world", "foo bar"])
    finally:
        await embedder.aclose()
    assert len(result) == 2


@respx.mock
async def test_anthropic_embeddings_dim() -> None:
    single_response = {
        "data": [{"embedding": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]}],
    }
    respx.post("https://api.voyageai.com/v1/embeddings").mock(
        return_value=httpx.Response(200, json=single_response)
    )
    embedder = AnthropicEmbeddings(api_key="k", model="voyage-3")
    try:
        d = await embedder.dim()
    finally:
        await embedder.aclose()
    assert d == 8


@respx.mock
async def test_anthropic_embeddings_empty_input() -> None:
    embedder = AnthropicEmbeddings(api_key="k")
    try:
        result = await embedder.embed([])
    finally:
        await embedder.aclose()
    assert result == []


# ---------------------------------------------------------------------------
# CohereEmbeddings HTTP shape (respx mocking httpx)
# ---------------------------------------------------------------------------

_COHERE_RESPONSE = {
    "id": "abc",
    "embeddings": {
        "float": [
            [0.11, 0.22, 0.33, 0.44],
            [0.55, 0.66, 0.77, 0.88],
        ]
    },
    "texts": ["hello world", "foo bar"],
    "meta": {},
}


@respx.mock
async def test_cohere_embeddings_shape() -> None:
    """CohereEmbeddings posts correct URL/body and parses the v2 response."""
    route = respx.post("https://api.cohere.com/v2/embed").mock(
        return_value=httpx.Response(200, json=_COHERE_RESPONSE)
    )

    embedder = CohereEmbeddings(api_key="test-cohere-key", model="embed-english-v3.0")
    try:
        result = await embedder.embed(["hello world", "foo bar"])
    finally:
        await embedder.aclose()

    assert route.called
    request = route.calls.last.request
    import json
    body = json.loads(request.content)
    assert body["model"] == "embed-english-v3.0"
    assert body["texts"] == ["hello world", "foo bar"]
    assert body["input_type"] == "search_document"
    assert "float" in body["embedding_types"]
    assert request.headers["authorization"] == "Bearer test-cohere-key"

    assert len(result) == 2
    assert result[0] == pytest.approx([0.11, 0.22, 0.33, 0.44])
    assert result[1] == pytest.approx([0.55, 0.66, 0.77, 0.88])


@respx.mock
async def test_cohere_embeddings_embed_batch_alias() -> None:
    respx.post("https://api.cohere.com/v2/embed").mock(
        return_value=httpx.Response(200, json=_COHERE_RESPONSE)
    )
    embedder = CohereEmbeddings(api_key="k")
    try:
        result = await embedder.embed_batch(["hello world", "foo bar"])
    finally:
        await embedder.aclose()
    assert len(result) == 2


@respx.mock
async def test_cohere_embeddings_dim() -> None:
    single_response = {
        "embeddings": {"float": [[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.0]]},
    }
    respx.post("https://api.cohere.com/v2/embed").mock(
        return_value=httpx.Response(200, json=single_response)
    )
    embedder = CohereEmbeddings(api_key="k")
    try:
        d = await embedder.dim()
    finally:
        await embedder.aclose()
    assert d == 10


@respx.mock
async def test_cohere_embeddings_empty_input() -> None:
    embedder = CohereEmbeddings(api_key="k")
    try:
        result = await embedder.embed([])
    finally:
        await embedder.aclose()
    assert result == []


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@respx.mock
async def test_anthropic_embeddings_raises_on_4xx() -> None:
    respx.post("https://api.voyageai.com/v1/embeddings").mock(
        return_value=httpx.Response(401, json={"message": "unauthorized"})
    )
    from oxenclaw.memory.embeddings import EmbeddingError

    embedder = AnthropicEmbeddings(api_key="bad")
    try:
        with pytest.raises(EmbeddingError, match="401"):
            await embedder.embed(["text"])
    finally:
        await embedder.aclose()


@respx.mock
async def test_cohere_embeddings_raises_on_4xx() -> None:
    respx.post("https://api.cohere.com/v2/embed").mock(
        return_value=httpx.Response(403, json={"message": "forbidden"})
    )
    from oxenclaw.memory.embeddings import EmbeddingError

    embedder = CohereEmbeddings(api_key="bad")
    try:
        with pytest.raises(EmbeddingError, match="403"):
            await embedder.embed(["text"])
    finally:
        await embedder.aclose()
