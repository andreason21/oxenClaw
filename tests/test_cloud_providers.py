"""Hosted cloud providers — openai / gemini / azure-openai.

Covers registration + catalog wiring, hosted credential resolution, and the
two auth shapes (Bearer for openai/gemini, `api-key` header + deployment URL
for azure). Network is stubbed at the aiohttp seam; tests assert on the URL +
headers the wrapper would have sent.
"""

from __future__ import annotations

import pytest

import oxenclaw.pi.providers  # registers all wrappers  # noqa: F401
from oxenclaw.agents.factory import CATALOG_PROVIDERS, PROVIDER_DEFAULT_MODELS, build_agent
from oxenclaw.pi import (
    Api,
    Context,
    Model,
    SimpleStreamOptions,
    get_provider_stream,
    text_message,
)
from oxenclaw.pi.auth import MissingCredential, resolve_api
from oxenclaw.pi.registry import InMemoryAuthStorage, is_inline_provider

# ─── registration + catalog ──────────────────────────────────────────


def test_cloud_providers_registered_and_in_catalog() -> None:
    for pid in ("openai", "gemini", "azure-openai"):
        assert pid in CATALOG_PROVIDERS
        assert get_provider_stream(pid) is not None
        assert pid in PROVIDER_DEFAULT_MODELS
        # Hosted, not inline.
        assert is_inline_provider(pid) is False


# ─── credential resolution ───────────────────────────────────────────


async def test_resolve_api_openai_default_base() -> None:
    auth = InMemoryAuthStorage({"openai": "sk-test"})
    api = await resolve_api(Model(id="gpt-4o-mini", provider="openai"), auth)
    assert api.base_url == "https://api.openai.com/v1"
    assert api.api_key == "sk-test"


async def test_resolve_api_gemini_default_base() -> None:
    auth = InMemoryAuthStorage({"gemini": "g-test"})
    api = await resolve_api(Model(id="gemini-2.5-flash", provider="gemini"), auth)
    assert api.base_url == "https://generativelanguage.googleapis.com/v1beta/openai"


async def test_resolve_api_missing_credential_raises() -> None:
    with pytest.raises(MissingCredential):
        await resolve_api(Model(id="gpt-4o-mini", provider="openai"), InMemoryAuthStorage({}))


async def test_resolve_api_azure_requires_base_url() -> None:
    auth = InMemoryAuthStorage({"azure-openai": "az-test"})
    # No base_url → clear ValueError (Azure endpoints are resource-specific).
    with pytest.raises(ValueError):
        await resolve_api(Model(id="gpt-4o-mini", provider="azure-openai"), auth)
    # With base_url → resolves.
    m = Model(
        id="gpt-4o-mini",
        provider="azure-openai",
        extra={"base_url": "https://res.openai.azure.com"},
    )
    api = await resolve_api(m, auth)
    assert api.base_url == "https://res.openai.azure.com"
    assert api.api_key == "az-test"


# ─── auth shapes (capturing aiohttp seam) ────────────────────────────


class _CapResp:
    def __init__(self, lines: list[str]) -> None:
        self.status = 200

        class _C:
            def __aiter__(self_inner):  # type: ignore[no-untyped-def]
                async def _gen():
                    for ln in lines:
                        yield ln.encode("utf-8")

                return _gen()

        self.content = _C()

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, *a):  # type: ignore[no-untyped-def]
        return False

    async def text(self) -> str:
        return ""


class _CapSession:
    def __init__(self, sink: dict, lines: list[str]) -> None:
        self._sink = sink
        self._lines = lines

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, *a):  # type: ignore[no-untyped-def]
        return False

    def post(self, url, *, json=None, headers=None):  # type: ignore[no-untyped-def]
        self._sink["url"] = url
        self._sink["json"] = json
        self._sink["headers"] = headers
        return _CapResp(self._lines)


class _CapAiohttp:
    def __init__(self, sink: dict, lines: list[str]) -> None:
        self._sink = sink
        self._lines = lines
        self.ClientConnectionError = ConnectionError

        class _T:
            def __init__(self, total=None):  # type: ignore[no-untyped-def]
                self.total = total

        self.ClientTimeout = _T

    def ClientSession(self, *a, **k):  # type: ignore[no-untyped-def]
        return _CapSession(self._sink, self._lines)


_DONE = ['data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}', "data: [DONE]"]


@pytest.fixture
def capture(monkeypatch):  # type: ignore[no-untyped-def]
    from oxenclaw.pi.providers import _openai_shared as shared

    sink: dict = {}
    monkeypatch.setattr(shared, "aiohttp", _CapAiohttp(sink, _DONE))
    return sink


async def _drain(stream):  # type: ignore[no-untyped-def]
    async for _ in stream:
        pass


async def test_openai_uses_bearer_auth(capture) -> None:
    fn = get_provider_stream("openai")
    model = Model(id="gpt-4o-mini", provider="openai")
    api = Api(base_url="https://api.openai.com/v1", api_key="sk-test")
    ctx = Context(model=model, api=api, messages=[text_message("hi")])
    await _drain(fn(ctx, SimpleStreamOptions()))
    assert capture["url"] == "https://api.openai.com/v1/chat/completions"
    assert capture["headers"]["Authorization"] == "Bearer sk-test"
    assert "api-key" not in capture["headers"]


async def test_azure_uses_api_key_header_and_deployment_url(capture) -> None:
    fn = get_provider_stream("azure-openai")
    model = Model(
        id="gpt-4o-mini",
        provider="azure-openai",
        extra={"base_url": "https://res.openai.azure.com", "azure_deployment": "my-dep",
               "api_version": "2024-10-21"},
    )
    api = Api(base_url="https://res.openai.azure.com", api_key="az-test")
    ctx = Context(model=model, api=api, messages=[text_message("hi")])
    await _drain(fn(ctx, SimpleStreamOptions()))
    assert capture["url"] == (
        "https://res.openai.azure.com/openai/deployments/my-dep/chat/completions"
        "?api-version=2024-10-21"
    )
    assert capture["headers"]["api-key"] == "az-test"
    assert "Authorization" not in capture["headers"]


async def test_azure_deployment_defaults_to_model_id(capture) -> None:
    fn = get_provider_stream("azure-openai")
    model = Model(
        id="gpt-4o", provider="azure-openai", extra={"base_url": "https://r.openai.azure.com"}
    )
    api = Api(base_url="https://r.openai.azure.com", api_key="az")
    ctx = Context(model=model, api=api, messages=[text_message("hi")])
    await _drain(fn(ctx, SimpleStreamOptions()))
    assert "/openai/deployments/gpt-4o/chat/completions?api-version=" in capture["url"]


async def test_azure_api_version_from_env(capture, monkeypatch) -> None:
    monkeypatch.setenv("OXENCLAW_AZURE_API_VERSION", "2025-01-01-preview")
    fn = get_provider_stream("azure-openai")
    model = Model(
        id="gpt-4o", provider="azure-openai", extra={"base_url": "https://r.openai.azure.com"}
    )
    api = Api(base_url="https://r.openai.azure.com", api_key="az")
    ctx = Context(model=model, api=api, messages=[text_message("hi")])
    await _drain(fn(ctx, SimpleStreamOptions()))
    assert capture["url"].endswith("?api-version=2025-01-01-preview")


# ─── factory wiring ──────────────────────────────────────────────────


def test_factory_builds_openai_agent() -> None:
    from oxenclaw.agents.pi_agent import PiAgent

    agent = build_agent(agent_id="a", provider="openai", api_key="sk-x")
    assert isinstance(agent, PiAgent)
    assert agent._model.provider == "openai"
    assert agent._model.id == "gpt-4o-mini"


def test_factory_builds_gemini_with_explicit_model() -> None:
    from oxenclaw.agents.pi_agent import PiAgent

    agent = build_agent(agent_id="a", provider="gemini", model="gemini-2.5-pro", api_key="g-x")
    assert isinstance(agent, PiAgent)
    assert agent._model.provider == "gemini"
    assert agent._model.id == "gemini-2.5-pro"
