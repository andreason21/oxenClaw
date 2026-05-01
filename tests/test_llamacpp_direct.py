"""Tests for the `llamacpp-direct` managed-server provider.

Two surfaces are exercised:

1. The `LlamaCppServerSpec` / cmd-builder / binary-discovery layer in
   `oxenclaw.pi.llamacpp_server.manager`. No subprocesses are spawned;
   we patch the binary path and verify argv assembly + cache_key
   identity.

2. The provider stream wrapper in
   `oxenclaw.pi.providers.llamacpp_direct`. We patch
   `get_default_server` to return a stub that records the spec and
   returns a base URL, then drive the shared OpenAI SSE wrapper through
   the same `_FakeAiohttpModule` pattern other tests use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import oxenclaw.pi.providers  # noqa: F401  (registers wrappers)
from oxenclaw.pi.llamacpp_server.manager import (
    LlamaCppServerError,
    LlamaCppServerSpec,
    _build_command,
    find_free_port,
    find_llama_server_binary,
)
from oxenclaw.pi.messages import UserMessage
from oxenclaw.pi.models import Api, Context, Model
from oxenclaw.pi.streaming import (
    ErrorEvent,
    SimpleStreamOptions,
    StopEvent,
    TextDeltaEvent,
    get_provider_stream,
)


def _user(text: str) -> UserMessage:
    return UserMessage(content=text)


# ─── Manager: binary discovery ────────────────────────────────────────


def test_find_binary_prefers_env_var(tmp_path: Path, monkeypatch) -> None:
    fake = tmp_path / "llama-server"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("OXENCLAW_LLAMACPP_BIN", str(fake))
    monkeypatch.delenv("LLAMA_SERVER_PATH", raising=False)
    monkeypatch.delenv("UNSLOTH_LLAMA_CPP_PATH", raising=False)

    found = find_llama_server_binary()
    assert found == fake


def test_find_binary_env_var_must_be_executable(tmp_path: Path, monkeypatch) -> None:
    fake = tmp_path / "llama-server"
    fake.write_text("not executable")
    monkeypatch.setenv("OXENCLAW_LLAMACPP_BIN", str(fake))

    with pytest.raises(LlamaCppServerError):
        find_llama_server_binary()


def test_find_binary_falls_back_to_which(monkeypatch) -> None:
    for env in ("OXENCLAW_LLAMACPP_BIN", "LLAMA_SERVER_PATH", "UNSLOTH_LLAMA_CPP_PATH"):
        monkeypatch.delenv(env, raising=False)

    with patch("shutil.which", return_value="/usr/local/bin/llama-server"):
        found = find_llama_server_binary()
    assert found == Path("/usr/local/bin/llama-server")


def test_find_binary_raises_when_nothing_found(monkeypatch) -> None:
    for env in ("OXENCLAW_LLAMACPP_BIN", "LLAMA_SERVER_PATH", "UNSLOTH_LLAMA_CPP_PATH"):
        monkeypatch.delenv(env, raising=False)

    with (
        patch("shutil.which", return_value=None),
        patch(
            "oxenclaw.pi.llamacpp_server.manager._candidate_install_dirs",
            return_value=[],
        ),
    ):
        with pytest.raises(LlamaCppServerError, match="not found"):
            find_llama_server_binary()


# ─── Manager: free port ───────────────────────────────────────────────


def test_find_free_port_returns_int_in_range() -> None:
    p = find_free_port()
    assert isinstance(p, int)
    assert 1024 <= p <= 65535


# ─── Manager: command assembly ────────────────────────────────────────


def test_build_command_includes_fast_preset_flags(tmp_path: Path) -> None:
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"\x00")
    spec = LlamaCppServerSpec(gguf_path=gguf, n_ctx=8192, n_gpu_layers=999)
    cmd = _build_command(Path("/bin/llama-server"), spec, port=12345)

    assert cmd[0] == "/bin/llama-server"
    assert "--port" in cmd and "12345" in cmd
    assert "-m" in cmd and str(gguf) in cmd
    assert "-c" in cmd and "8192" in cmd
    assert "-ngl" in cmd and "999" in cmd
    # Studio's fast-preset:
    assert "--flash-attn" in cmd and "on" in cmd
    assert "--no-context-shift" in cmd
    assert "--jinja" in cmd
    assert "--parallel" in cmd and "1" in cmd


def test_build_command_appends_extra_args(tmp_path: Path) -> None:
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"\x00")
    spec = LlamaCppServerSpec(
        gguf_path=gguf,
        extra_args=("--cache-type-k", "q4_0", "--mlock"),
    )
    cmd = _build_command(Path("/bin/llama-server"), spec, port=1)
    # extra_args land after the defaults so they win on conflict.
    assert cmd[-3:] == ["--cache-type-k", "q4_0", "--mlock"]


def test_embedding_spec_emits_embedding_flag_and_drops_chat_only(tmp_path: Path) -> None:
    """`embedding=True` switches the cmd builder into embedding mode:
    `--embedding` is emitted and chat-only flags (`--no-context-shift`,
    `--jinja`) are dropped (some embedding builds reject them)."""
    gguf = tmp_path / "embed.gguf"
    gguf.write_bytes(b"\x00")
    spec = LlamaCppServerSpec(
        gguf_path=gguf,
        n_ctx=8192,
        embedding=True,
        pooling="mean",
    )
    cmd = _build_command(Path("/bin/llama-server"), spec, port=42)
    assert "--embedding" in cmd
    assert "--pooling" in cmd and "mean" in cmd
    assert "--no-context-shift" not in cmd
    assert "--jinja" not in cmd
    # flash-attn still on for embeddings.
    assert "--flash-attn" in cmd and "on" in cmd


def test_chat_and_embedding_servers_are_independent(tmp_path: Path) -> None:
    from oxenclaw.pi.llamacpp_server import (
        get_default_server,
        get_embedding_server,
    )

    chat = get_default_server()
    embed = get_embedding_server()
    # Distinct singletons so chat reload doesn't kick the embedding
    # server (and vice versa).
    assert chat is not embed
    # Calling the accessors twice returns the same instances (no
    # accidental rebuild on every call).
    assert chat is get_default_server()
    assert embed is get_embedding_server()


def test_spec_cache_key_distinguishes_meaningful_changes(tmp_path: Path) -> None:
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"\x00")
    a = LlamaCppServerSpec(gguf_path=gguf, n_ctx=4096)
    b = LlamaCppServerSpec(gguf_path=gguf, n_ctx=4096)
    c = LlamaCppServerSpec(gguf_path=gguf, n_ctx=8192)
    assert a.cache_key() == b.cache_key()
    assert a.cache_key() != c.cache_key()


# ─── Provider: missing-config error path ──────────────────────────────


@pytest.mark.asyncio
async def test_provider_errors_when_no_gguf_configured(monkeypatch) -> None:
    monkeypatch.delenv("OXENCLAW_LLAMACPP_GGUF", raising=False)
    fn = get_provider_stream("llamacpp-direct")
    model = Model(id="local-gguf", provider="llamacpp-direct")
    ctx = Context(
        model=model,
        api=Api(base_url="managed://llamacpp-direct"),
        messages=[_user("hi")],
    )
    events: list[Any] = []
    async for ev in fn(ctx, SimpleStreamOptions()):
        events.append(ev)

    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert errors and "no GGUF path configured" in errors[0].message
    assert any(isinstance(e, StopEvent) for e in events)


# ─── Provider: end-to-end with mocked manager + mocked aiohttp ────────


class _StubServer:
    """Stub `LlamaCppServer` that records the spec and serves a fake URL."""

    def __init__(self, base_url: str = "http://127.0.0.1:55555/v1") -> None:
        self.base_url = base_url
        self.last_spec: LlamaCppServerSpec | None = None

    def ensure_loaded(
        self, spec: LlamaCppServerSpec, *, health_timeout_s: float | None = None
    ) -> str:
        self.last_spec = spec
        return self.base_url


# Reuse the fake-aiohttp scaffolding from test_pi_providers — duplicated
# here so this file is independently runnable.


class _FakeContent:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __aiter__(self):  # type: ignore[no-untyped-def]
        async def _gen():
            for ln in self._lines:
                yield ln.encode("utf-8")

        return _gen()


class _FakeResp:
    def __init__(self, lines: list[str], status: int = 200) -> None:
        self.status = status
        self.content = _FakeContent(lines)

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, *a):  # type: ignore[no-untyped-def]
        return False

    async def text(self) -> str:
        return ""


class _FakeSession:
    def __init__(self, lines: list[str], status: int = 200) -> None:
        self._lines = lines
        self._status = status
        self.last_url: str | None = None
        self.last_payload: dict[str, Any] | None = None

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, *a):  # type: ignore[no-untyped-def]
        return False

    def post(self, url, *, json=None, headers=None):  # type: ignore[no-untyped-def]
        self.last_url = url
        self.last_payload = json
        return _FakeResp(self._lines, self._status)


class _FakeAiohttpModule:
    def __init__(self, lines: list[str], status: int) -> None:
        self._lines = lines
        self._status = status
        self.ClientConnectionError = ConnectionError
        self._sessions: list[_FakeSession] = []

        class _ClientTimeout:
            def __init__(self, total=None):  # type: ignore[no-untyped-def]
                self.total = total

        self.ClientTimeout = _ClientTimeout

    def ClientSession(self, *a, **k):  # type: ignore[no-untyped-def]
        s = _FakeSession(self._lines, self._status)
        self._sessions.append(s)
        return s


@pytest.mark.asyncio
async def test_provider_spawns_server_and_streams(tmp_path: Path, monkeypatch) -> None:
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"\x00")
    monkeypatch.setenv("OXENCLAW_LLAMACPP_GGUF", str(gguf))

    stub = _StubServer(base_url="http://127.0.0.1:55555/v1")
    monkeypatch.setattr(
        "oxenclaw.pi.providers.llamacpp_direct.get_default_server",
        lambda: stub,
    )

    chunks = [
        'data: {"choices":[{"delta":{"content":"he"}}]}',
        'data: {"choices":[{"delta":{"content":"llo"}}]}',
        'data: {"choices":[{"finish_reason":"stop"}]}',
        'data: {"usage":{"prompt_tokens":3,"completion_tokens":2}}',
        "data: [DONE]",
    ]
    fake_aiohttp = _FakeAiohttpModule(chunks, status=200)
    from oxenclaw.pi.providers import _openai_shared as shared

    monkeypatch.setattr(shared, "aiohttp", fake_aiohttp)

    fn = get_provider_stream("llamacpp-direct")
    model = Model(id="qwen3-coder", provider="llamacpp-direct")
    ctx = Context(
        model=model,
        api=Api(base_url="managed://llamacpp-direct"),
        messages=[_user("hi")],
    )
    events: list[Any] = []
    async for ev in fn(ctx, SimpleStreamOptions()):
        events.append(ev)

    text = "".join(e.delta for e in events if isinstance(e, TextDeltaEvent))
    assert text == "hello"
    assert any(isinstance(e, StopEvent) for e in events)

    # The provider must rewrite the base URL to whatever the manager
    # returned, not the placeholder `managed://...` from inline config.
    assert fake_aiohttp._sessions
    assert fake_aiohttp._sessions[0].last_url == "http://127.0.0.1:55555/v1/chat/completions"

    # And the manager must have been handed the env-configured GGUF.
    assert stub.last_spec is not None
    assert stub.last_spec.gguf_path == gguf


@pytest.mark.asyncio
async def test_provider_propagates_spawn_failure(tmp_path: Path, monkeypatch) -> None:
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"\x00")
    monkeypatch.setenv("OXENCLAW_LLAMACPP_GGUF", str(gguf))

    class _FailingServer:
        def ensure_loaded(self, spec, *, health_timeout_s=None):  # type: ignore[no-untyped-def]
            raise LlamaCppServerError("simulated spawn failure: tail xyz")

    monkeypatch.setattr(
        "oxenclaw.pi.providers.llamacpp_direct.get_default_server",
        lambda: _FailingServer(),
    )

    fn = get_provider_stream("llamacpp-direct")
    model = Model(id="local-gguf", provider="llamacpp-direct")
    ctx = Context(
        model=model,
        api=Api(base_url="managed://llamacpp-direct"),
        messages=[_user("hi")],
    )
    events: list[Any] = []
    async for ev in fn(ctx, SimpleStreamOptions()):
        events.append(ev)

    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert errors and "simulated spawn failure" in errors[0].message
    # Spawn failures are not transient — the provider must mark
    # them non-retryable so the run loop doesn't pound on them.
    assert errors[0].retryable is False


# ─── Provider: model.extra overrides env ──────────────────────────────


@pytest.mark.asyncio
async def test_model_extra_overrides_env(tmp_path: Path, monkeypatch) -> None:
    env_gguf = tmp_path / "from_env.gguf"
    env_gguf.write_bytes(b"\x00")
    extra_gguf = tmp_path / "from_extra.gguf"
    extra_gguf.write_bytes(b"\x00")
    monkeypatch.setenv("OXENCLAW_LLAMACPP_GGUF", str(env_gguf))

    stub = _StubServer()
    monkeypatch.setattr(
        "oxenclaw.pi.providers.llamacpp_direct.get_default_server",
        lambda: stub,
    )

    fake_aiohttp = _FakeAiohttpModule(
        ['data: {"choices":[{"finish_reason":"stop"}]}', "data: [DONE]"], 200
    )
    from oxenclaw.pi.providers import _openai_shared as shared

    monkeypatch.setattr(shared, "aiohttp", fake_aiohttp)

    fn = get_provider_stream("llamacpp-direct")
    model = Model(
        id="local-gguf",
        provider="llamacpp-direct",
        extra={"gguf_path": str(extra_gguf), "n_ctx": 32768},
    )
    ctx = Context(
        model=model,
        api=Api(base_url="managed://llamacpp-direct"),
        messages=[_user("hi")],
    )
    async for _ in fn(ctx, SimpleStreamOptions()):
        pass

    assert stub.last_spec is not None
    assert stub.last_spec.gguf_path == extra_gguf
    assert stub.last_spec.n_ctx == 32768
