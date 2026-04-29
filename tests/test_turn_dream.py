"""Per-turn LLM dreamer that fills the gap the regex backstop can't.

Locks the contract that matters operationally:
  - default ON (turn_dream is the path that makes arbitrary
    free-form facts reach memory; default OFF made memory look
    broken on anything but the handful of regex shapes)
  - skips ultra-short messages and questions handled by the LLM
    prompt itself
  - dedupes against `already_saved` (the regex layer's output)
  - drops low-confidence facts
"""

from __future__ import annotations

from pathlib import Path

import oxenclaw.pi.providers  # noqa: F401
from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.memory.retriever import MemoryRetriever
from oxenclaw.memory.turn_dream import TurnDreamConfig, dream_turn
from oxenclaw.pi import (
    InMemoryAuthStorage,
    Model,
    register_provider_stream,
    resolve_api,
)
from oxenclaw.pi.streaming import StopEvent, TextDeltaEvent
from tests._memory_stubs import StubEmbeddings


def _retriever(tmp_path: Path) -> MemoryRetriever:
    paths = OxenclawPaths(home=tmp_path)
    paths.ensure_home()
    return MemoryRetriever.for_root(paths, StubEmbeddings())


def _model(provider: str) -> Model:
    return Model(id="m", provider=provider, max_output_tokens=256, extra={"base_url": "x"})


def _api(provider: str):  # type: ignore[no-untyped-def]
    return lambda m: resolve_api(m, InMemoryAuthStorage({provider: "x"}))  # type: ignore[dict-item]


def test_default_config_is_disabled_in_dataclass() -> None:
    """The dataclass default is OFF so direct PiAgent construction
    (tests, library use) never silently consumes an extra LLM call.
    Production wiring lives in `gateway_cmd`, which defaults the
    `OXENCLAW_TURN_DREAM` env to "1" and explicitly constructs
    `TurnDreamConfig(enabled=True, ...)`."""
    cfg = TurnDreamConfig()
    assert cfg.enabled is False


async def test_skipped_when_disabled(tmp_path: Path) -> None:
    retriever = _retriever(tmp_path)
    try:
        result = await dream_turn(
            user_text="민수는 우리 형이야",
            memory=retriever,
            sub_model=_model("never_called"),
            api_resolver=_api("never_called"),
            config=TurnDreamConfig(enabled=False),
        )
        assert result.skipped_reason == "turn-dream disabled"
        assert result.saved_facts == []
    finally:
        await retriever.aclose()


async def test_skipped_when_text_too_short(tmp_path: Path) -> None:
    retriever = _retriever(tmp_path)
    try:
        result = await dream_turn(
            user_text="ok",
            memory=retriever,
            sub_model=_model("never_called"),
            api_resolver=_api("never_called"),
            config=TurnDreamConfig(enabled=True, min_chars=8),
        )
        assert result.skipped_reason is not None
        assert "too short" in result.skipped_reason
        assert result.saved_facts == []
    finally:
        await retriever.aclose()


async def test_saves_extracted_facts(tmp_path: Path) -> None:
    """Happy path: model returns a JSON facts payload → inbox grows."""

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        yield TextDeltaEvent(
            delta='{"facts": [{"text": "사용자의 형은 민수이다.", '
            '"confidence": 0.9, "tags": ["family"]}]}'
        )
        yield StopEvent(reason="end_turn")

    register_provider_stream("td_ok", fake_stream)
    retriever = _retriever(tmp_path)
    try:
        result = await dream_turn(
            user_text="나의 형 이름은 민수야",
            memory=retriever,
            sub_model=_model("td_ok"),
            api_resolver=_api("td_ok"),
            config=TurnDreamConfig(enabled=True),
        )
        assert result.error is None
        assert result.saved_facts == ["사용자의 형은 민수이다."]
        # Inbox file actually written.
        assert "민수" in retriever.inbox_path.read_text(encoding="utf-8")
    finally:
        await retriever.aclose()


async def test_skips_already_saved_facts(tmp_path: Path) -> None:
    """If the regex backstop already emitted the same sentence, the
    LLM layer must not re-save it."""

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        yield TextDeltaEvent(
            delta='{"facts": [{"text": "사용자의 형은 민수이다.", "confidence": 0.9}]}'
        )
        yield StopEvent(reason="end_turn")

    register_provider_stream("td_dup", fake_stream)
    retriever = _retriever(tmp_path)
    try:
        result = await dream_turn(
            user_text="나의 형 이름은 민수야",
            memory=retriever,
            sub_model=_model("td_dup"),
            api_resolver=_api("td_dup"),
            config=TurnDreamConfig(enabled=True),
            already_saved=["사용자의 형은 민수이다."],
        )
        assert result.saved_facts == []
    finally:
        await retriever.aclose()


async def test_drops_low_confidence_facts(tmp_path: Path) -> None:
    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        yield TextDeltaEvent(
            delta='{"facts": ['
            '{"text": "high conf fact", "confidence": 0.9},'
            '{"text": "low conf fact", "confidence": 0.2}'
            "]}"
        )
        yield StopEvent(reason="end_turn")

    register_provider_stream("td_conf", fake_stream)
    retriever = _retriever(tmp_path)
    try:
        result = await dream_turn(
            user_text="some statement that triggers fact extraction",
            memory=retriever,
            sub_model=_model("td_conf"),
            api_resolver=_api("td_conf"),
            config=TurnDreamConfig(enabled=True, min_confidence=0.5),
        )
        assert result.saved_facts == ["high conf fact"]
    finally:
        await retriever.aclose()


async def test_empty_facts_array_is_not_an_error(tmp_path: Path) -> None:
    """Most messages have no durable fact — the LLM correctly
    returning {"facts": []} must not be treated as an error."""

    async def fake_stream(ctx, opts):  # type: ignore[no-untyped-def]
        yield TextDeltaEvent(delta='{"facts": []}')
        yield StopEvent(reason="end_turn")

    register_provider_stream("td_empty", fake_stream)
    retriever = _retriever(tmp_path)
    try:
        result = await dream_turn(
            user_text="오늘 날씨 어때?",
            memory=retriever,
            sub_model=_model("td_empty"),
            api_resolver=_api("td_empty"),
            config=TurnDreamConfig(enabled=True),
        )
        assert result.error is None
        assert result.saved_facts == []
        assert result.skipped_reason == "no durable facts extracted"
    finally:
        await retriever.aclose()
