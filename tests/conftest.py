"""Top-level pytest fixtures shared by every test module.

Currently only one job: scrub `OXENCLAW_LLAMACPP_*` env vars from the
test session. Operators who set up `llamacpp-direct` on their own
machine end up with `OXENCLAW_LLAMACPP_BIN` + `OXENCLAW_LLAMACPP_GGUF`
in their shell (e.g. via `~/.bashrc` sourcing `~/.oxenclaw/env`).
Without this fixture, those vars leak into pytest, the
`resolve_default_local_provider()` resolver picks `llamacpp-direct`
on a developer's box but `ollama` in CI, and tests that assert
provider routing flap. Scrub at session start for deterministic
behaviour everywhere.
"""

from __future__ import annotations

import pytest

_OXENCLAW_HOST_ENV_KEYS = (
    "OXENCLAW_LLAMACPP_BIN",
    "OXENCLAW_LLAMACPP_GGUF",
    "LLAMA_SERVER_PATH",
    "OXENCLAW_LLAMACPP_NGL",
    "OXENCLAW_LLAMACPP_CTX",
    "OXENCLAW_LLAMACPP_THREADS",
    "OXENCLAW_LLAMACPP_PARALLEL",
    "OXENCLAW_LLAMACPP_EXTRA_ARGS",
    "OXENCLAW_LLAMACPP_EMBED_GGUF",
    "OXENCLAW_LLAMACPP_EMBED_CTX",
    "OXENCLAW_LLAMACPP_EMBED_NGL",
    "OXENCLAW_LLAMACPP_EMBED_POOLING",
)


@pytest.fixture(autouse=True)
def _scrub_oxenclaw_host_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset every host-llamacpp env var before each test runs."""
    for key in _OXENCLAW_HOST_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
