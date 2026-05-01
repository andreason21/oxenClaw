"""`cache_control_breakpoints` gating on `Model.supports_prompt_cache`.

Cache markers are an Anthropic-specific feature; emitting them on
local providers (Ollama, llama.cpp, vLLM) is at best ignored and at
worst breaks `--jinja` template rendering. This test pins the gate at
`run_attempt` so a future refactor can't silently regress.
"""

from __future__ import annotations

import oxenclaw.pi.providers  # noqa: F401  — registers stream wrappers
from oxenclaw.pi import (
    InMemoryAuthStorage,
    Model,
    register_provider_stream,
    resolve_api,
)
from oxenclaw.pi.run import RuntimeConfig
from oxenclaw.pi.run.attempt import run_attempt
from oxenclaw.pi.streaming import StopEvent, TextDeltaEvent


async def _drive(*, supports_prompt_cache: bool, configured_breakpoints: int = 4) -> int:
    """Drives one `run_attempt` and returns the breakpoints the provider saw."""
    seen: dict[str, int] = {}

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        seen["breakpoints"] = ctx.cache_control_breakpoints
        yield TextDeltaEvent(delta="ok")
        yield StopEvent(reason="end_turn")

    register_provider_stream("cachegate", fake_stream)
    model = Model(
        id="m",
        provider="cachegate",  # type: ignore[arg-type]
        max_output_tokens=64,
        supports_prompt_cache=supports_prompt_cache,
        extra={"base_url": "x"},
    )
    api = await resolve_api(
        model,
        InMemoryAuthStorage({"cachegate": "x"}),  # type: ignore[dict-item]
    )
    cfg = RuntimeConfig(cache_control_breakpoints=configured_breakpoints)
    await run_attempt(model=model, api=api, system=None, messages=[], tools=[], config=cfg)
    return seen["breakpoints"]


async def test_cache_breakpoints_zeroed_for_local_provider() -> None:
    """Default catalog model (`supports_prompt_cache=False`) → breakpoints=0."""
    bp = await _drive(supports_prompt_cache=False, configured_breakpoints=4)
    assert bp == 0


async def test_cache_breakpoints_passed_through_for_anthropic_class() -> None:
    """Models with cache support honour the operator-configured value."""
    bp = await _drive(supports_prompt_cache=True, configured_breakpoints=4)
    assert bp == 4


async def test_cache_breakpoints_zero_when_operator_disables() -> None:
    """Operator can still turn it off explicitly even on cache-capable models."""
    bp = await _drive(supports_prompt_cache=True, configured_breakpoints=0)
    assert bp == 0
