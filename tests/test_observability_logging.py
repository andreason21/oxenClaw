"""Tests for oxenclaw.observability.logging."""

from __future__ import annotations

import asyncio
import io
import json
import logging

import pytest

from oxenclaw.observability import (
    configure_logging,
    correlation_scope,
    get_context,
    new_correlation_id,
)


def _make_logger(stream: io.StringIO, fmt: str) -> logging.Logger:
    configure_logging(level="DEBUG", fmt=fmt, stream=stream)
    return logging.getLogger("oxenclaw.test.logging")


def test_new_correlation_id_is_short_hex():
    a = new_correlation_id()
    b = new_correlation_id()
    assert len(a) == 12
    assert a != b
    int(a, 16)  # valid hex


def test_correlation_scope_layers():
    with correlation_scope(trace_id="abc"):
        assert get_context() == {"trace_id": "abc"}
        with correlation_scope(rpc="x.y", agent_id="bot"):
            ctx = get_context()
            assert ctx["trace_id"] == "abc"
            assert ctx["rpc"] == "x.y"
            assert ctx["agent_id"] == "bot"
        assert get_context() == {"trace_id": "abc"}
    assert get_context() == {}


def test_correlation_scope_overrides_existing_keys():
    with correlation_scope(trace_id="outer"):
        with correlation_scope(trace_id="inner"):
            assert get_context()["trace_id"] == "inner"
        assert get_context()["trace_id"] == "outer"


def test_json_formatter_emits_json_with_context():
    stream = io.StringIO()
    log = _make_logger(stream, fmt="json")
    with correlation_scope(trace_id="t1", rpc="chat.send"):
        log.info("hi there")
    line = stream.getvalue().strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["message"] == "hi there"
    assert payload["trace_id"] == "t1"
    assert payload["rpc"] == "chat.send"
    assert payload["logger"].endswith(".test.logging")


def test_json_formatter_includes_exc_info():
    stream = io.StringIO()
    log = _make_logger(stream, fmt="json")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("oops")
    line = stream.getvalue().strip().splitlines()[-1]
    payload = json.loads(line)
    assert "exc_info" in payload
    assert "ValueError" in payload["exc_info"]


def test_human_formatter_appends_context_suffix():
    stream = io.StringIO()
    log = _make_logger(stream, fmt="human")
    with correlation_scope(trace_id="abc"):
        log.warning("look at this")
    out = stream.getvalue()
    assert "WARNING" in out
    assert "look at this" in out
    assert "[trace_id=abc]" in out


def test_human_formatter_no_suffix_when_no_context():
    stream = io.StringIO()
    log = _make_logger(stream, fmt="human")
    log.info("nothing here")
    out = stream.getvalue()
    assert "nothing here" in out
    assert "[" not in out.split("nothing here", 1)[1]  # no suffix after msg


def test_env_var_picks_format(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setenv("OXENCLAW_LOG_FORMAT", "json")
    configure_logging(level="INFO", stream=stream)
    log = logging.getLogger("oxenclaw.test.envfmt")
    log.info("msg")
    line = stream.getvalue().strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["message"] == "msg"


@pytest.mark.asyncio
async def test_correlation_scope_isolated_across_tasks():
    """Two concurrent tasks don't see each other's context (contextvars
    are per-task)."""
    seen: dict[str, str] = {}

    async def worker(name: str, value: str) -> None:
        with correlation_scope(trace_id=value):
            await asyncio.sleep(0.01)
            seen[name] = get_context()["trace_id"]

    await asyncio.gather(worker("a", "tA"), worker("b", "tB"))
    assert seen == {"a": "tA", "b": "tB"}
