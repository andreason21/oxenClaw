"""Skip every integration test unless the runner opts in.

Set `OLLAMA_INTEGRATION=1` (or any truthy value) to run the suite. We default
to skipping so CI and casual `pytest` runs stay hermetic — these tests
exercise a live local Ollama, which CI machines don't have and which are
slow (each call is multiple seconds).

Override the target model/endpoint with:
  - `SAMPYCLAW_OLLAMA_MODEL`     (default: gemma4:latest)
  - `SAMPYCLAW_OLLAMA_BASE_URL`  (default: http://127.0.0.1:11434/v1)
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request

import pytest

DEFAULT_MODEL = "gemma4:latest"
DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"


def _truthy(value: str | None) -> bool:
    return bool(value) and value.lower() not in ("0", "false", "no", "off", "")


def _ollama_reachable(base_url: str) -> bool:
    health = base_url.replace("/v1", "") + "/api/tags"
    try:
        with urllib.request.urlopen(health, timeout=3) as resp:
            resp.read(1)
        return True
    except (urllib.error.URLError, OSError):
        return False


def pytest_collection_modifyitems(config, items):  # type: ignore[no-untyped-def]
    if not _truthy(os.environ.get("OLLAMA_INTEGRATION")):
        skip_marker = pytest.mark.skip(reason="set OLLAMA_INTEGRATION=1 to run live-LLM tests")
        for item in items:
            if "integration" in str(item.fspath):
                item.add_marker(skip_marker)
        return

    base_url = os.environ.get("SAMPYCLAW_OLLAMA_BASE_URL", DEFAULT_BASE_URL)
    if not _ollama_reachable(base_url):
        skip_marker = pytest.mark.skip(reason=f"Ollama not reachable at {base_url}")
        for item in items:
            if "integration" in str(item.fspath):
                item.add_marker(skip_marker)


@pytest.fixture(scope="session")
def ollama_base_url() -> str:
    return os.environ.get("SAMPYCLAW_OLLAMA_BASE_URL", DEFAULT_BASE_URL)


@pytest.fixture(scope="session")
def ollama_model() -> str:
    return os.environ.get("SAMPYCLAW_OLLAMA_MODEL", DEFAULT_MODEL)
