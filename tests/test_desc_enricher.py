"""Tests for the LLM-driven skill description enricher.

We exercise the parts that don't need a real LLM:

- `render_for_prompt` — deterministic templating used by the loader.
- `parse_llm_response` — leniency around code fences / leading prose.
- `enrich_skill_description` — cache hit + cache miss with a stub
  `run_agent_turn` that returns canned JSON.

The actual provider call lives behind `oxenclaw.pi.run.run_agent_turn`,
which we patch with `monkeypatch` so the test never opens a socket.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from oxenclaw.clawhub import desc_enricher
from oxenclaw.clawhub.desc_enricher import (
    EnrichedDescription,
    cache_path_for_skill,
    content_hash,
    enrich_skill_description,
    is_disabled,
    load_cached,
    parse_llm_response,
    render_for_prompt,
)

# ────────────────────────────────────────────────────────────────────────────
# render_for_prompt
# ────────────────────────────────────────────────────────────────────────────


def test_render_falls_back_when_no_enrichment() -> None:
    out = render_for_prompt("Plain summary.", None)
    assert out == "Plain summary."


def test_render_emits_all_three_sections() -> None:
    enriched = EnrichedDescription(
        when_use=["the user asks for X", "you have a Y handy"],
        when_skip=["the user wants Z (use other_tool)"],
        alternatives={"other_tool": "covers Z"},
    )
    out = render_for_prompt("Base.", enriched)
    assert "Base." in out
    assert "WHEN TO USE:" in out
    assert "WHEN NOT TO USE:" in out
    assert "ALTERNATIVES:" in out
    assert "other_tool (covers Z)" in out


def test_render_drops_empty_blocks() -> None:
    enriched = EnrichedDescription(when_use=["only this"])
    out = render_for_prompt("Base.", enriched)
    assert "WHEN TO USE:" in out
    assert "WHEN NOT TO USE:" not in out
    assert "ALTERNATIVES:" not in out


# ────────────────────────────────────────────────────────────────────────────
# parse_llm_response
# ────────────────────────────────────────────────────────────────────────────


def test_parse_plain_json() -> None:
    raw = '{"when_use":["a"],"when_skip":["b"],"alternatives":{"x":"y"}}'
    parsed = parse_llm_response(raw)
    assert parsed is not None
    assert parsed.when_use == ["a"]
    assert parsed.when_skip == ["b"]
    assert parsed.alternatives == {"x": "y"}


def test_parse_strips_code_fences() -> None:
    raw = '```json\n{"when_use":["a"]}\n```'
    parsed = parse_llm_response(raw)
    assert parsed is not None
    assert parsed.when_use == ["a"]


def test_parse_handles_leading_prose() -> None:
    raw = (
        "Sure! Here's the JSON you asked for:\n\n"
        '{"when_use":["a"],"when_skip":[]}\n\n'
        "Hope that helps."
    )
    parsed = parse_llm_response(raw)
    assert parsed is not None
    assert parsed.when_use == ["a"]


def test_parse_returns_none_on_garbage() -> None:
    assert parse_llm_response("") is None
    assert parse_llm_response("not json at all") is None
    assert parse_llm_response("{invalid: json,}") is None


# ────────────────────────────────────────────────────────────────────────────
# enrich_skill_description — cache + LLM stub
# ────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def skill_dir(tmp_path: Path) -> Path:
    p = tmp_path / "skill"
    p.mkdir()
    return p


def _stub_turn_factory(
    response_text: str,
) -> Any:
    """Build a stub `run_agent_turn` that returns a canned response."""

    from oxenclaw.pi import AssistantMessage, TextContent

    class _Result:
        def __init__(self) -> None:
            self.final_message = AssistantMessage(content=[TextContent(text=response_text)])

    async def _fake_run_agent_turn(
        *,
        model: Any,
        api: Any,
        system: str | None,
        history: list[Any],
        tools: list[Any],
        config: Any,
        on_event: Any | None = None,
    ) -> Any:
        return _Result()

    return _fake_run_agent_turn


@pytest.fixture
def patch_pi(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch resolve_api + run_agent_turn so no network call happens.

    Returns a setter for the canned response so individual tests can
    override what the "LLM" produces.
    """
    state: dict[str, Any] = {"response": ""}

    async def _fake_resolve_api(model: Any, auth: Any) -> Any:
        return object()

    def _set_response(text: str) -> None:
        state["response"] = text

    async def _fake_run_agent_turn(**kwargs: Any) -> Any:
        return await _stub_turn_factory(state["response"])(**kwargs)

    monkeypatch.setattr("oxenclaw.pi.auth.resolve_api", _fake_resolve_api)
    monkeypatch.setattr("oxenclaw.pi.run.run_agent_turn", _fake_run_agent_turn)
    return _set_response


def test_enrich_writes_cache_and_returns_record(
    skill_dir: Path,
    patch_pi: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(desc_enricher.ENV_DISABLE, raising=False)
    patch_pi(
        '{"when_use":["the user asks for X"],'
        ' "when_skip":["unrelated requests"],'
        ' "alternatives":{"web_search":"general queries"}}'
    )

    out = asyncio.run(
        enrich_skill_description(
            skill_dir=skill_dir,
            name="thing",
            description="Do a thing.",
            body="# Thing\nUsage: thing arg",
            model=object(),
            auth=object(),
        )
    )
    assert out is not None
    assert out.when_use == ["the user asks for X"]
    assert out.alternatives == {"web_search": "general queries"}

    cached = load_cached(skill_dir)
    assert cached is not None
    assert cached.enriched.when_use == ["the user asks for X"]
    assert cached.content_hash == content_hash("thing", "Do a thing.", "# Thing\nUsage: thing arg")


def test_enrich_cache_hit_skips_llm(
    skill_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-populated cache: enrich must NOT attempt to import pi runtime."""
    monkeypatch.delenv(desc_enricher.ENV_DISABLE, raising=False)
    # Plant a cache record matching the content hash we'll request.
    cache_dir = skill_dir / ".clawhub"
    cache_dir.mkdir()
    h = content_hash("thing", "desc", "body")
    record = (
        f'{{"content_hash": "{h}", "model_id": "stub", '
        '"enriched": {"when_use":["cached!"],"when_skip":[],"alternatives":{}}}'
    )
    (cache_dir / desc_enricher.ENRICHED_FILE_NAME).write_text(record)

    # If the cache miss path runs we'd hit a non-existent pi.auth path
    # because we did NOT install the patch. The cache hit should bypass it.
    out = asyncio.run(
        enrich_skill_description(
            skill_dir=skill_dir,
            name="thing",
            description="desc",
            body="body",
            model=object(),
            auth=object(),
        )
    )
    assert out is not None
    assert out.when_use == ["cached!"]


def test_enrich_disabled_returns_none(
    skill_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(desc_enricher.ENV_DISABLE, "0")
    assert is_disabled() is True
    out = asyncio.run(
        enrich_skill_description(
            skill_dir=skill_dir,
            name="thing",
            description="desc",
            body="body",
            model=object(),
            auth=object(),
        )
    )
    assert out is None


def test_enrich_malformed_response_returns_none(
    skill_dir: Path,
    patch_pi: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(desc_enricher.ENV_DISABLE, raising=False)
    patch_pi("not json at all — sorry")
    out = asyncio.run(
        enrich_skill_description(
            skill_dir=skill_dir,
            name="thing",
            description="desc",
            body="body",
            model=object(),
            auth=object(),
        )
    )
    assert out is None
    # No cache file should have been written for a failed parse.
    assert not cache_path_for_skill(skill_dir).exists()
