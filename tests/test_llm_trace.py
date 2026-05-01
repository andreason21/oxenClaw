"""Wire-level LLM trace — exercises the env-flag gate, file sink, and
truncation. These are the behaviours operators rely on when debugging
"why didn't the model call the tool?" — losing them means we're back
to flying blind."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oxenclaw.observability import llm_trace


def _read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_disabled_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OXENCLAW_LLM_TRACE", raising=False)
    monkeypatch.setenv("OXENCLAW_LLM_TRACE_FILE", str(tmp_path / "trace.jsonl"))
    llm_trace.log_request(
        request_id="r1",
        provider="ollama",
        model_id="qwen3.5:9b",
        url="http://x/v1/chat/completions",
        payload={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert not (tmp_path / "trace.jsonl").exists()


def test_request_response_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sink = tmp_path / "trace.jsonl"
    monkeypatch.setenv("OXENCLAW_LLM_TRACE", "1")
    monkeypatch.setenv("OXENCLAW_LLM_TRACE_FILE", str(sink))

    rid = llm_trace.new_request_id()
    llm_trace.log_request(
        request_id=rid,
        provider="ollama",
        model_id="qwen3.5:9b",
        url="http://x/v1/chat/completions",
        payload={
            "model": "qwen3.5:9b",
            "messages": [{"role": "user", "content": "what time is it?"}],
            "tools": [{"type": "function", "function": {"name": "get_time"}}],
        },
    )
    llm_trace.log_response(
        request_id=rid,
        provider="ollama",
        model_id="qwen3.5:9b",
        content="",
        tool_calls=[
            {"id": "call_1", "name": "get_time", "arguments": "{}"},
        ],
        finish_reason="tool_calls",
        usage={"prompt_tokens": 50, "completion_tokens": 10},
        duration_ms=123.4,
    )
    lines = _read_lines(sink)
    assert [r["event"] for r in lines] == ["request", "response"]
    assert lines[0]["request_id"] == rid == lines[1]["request_id"]
    assert lines[0]["payload"]["tools"][0]["function"]["name"] == "get_time"
    assert lines[1]["tool_calls"][0]["name"] == "get_time"
    assert lines[1]["finish_reason"] == "tool_calls"


def test_truncation_clamps_huge_strings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sink = tmp_path / "trace.jsonl"
    monkeypatch.setenv("OXENCLAW_LLM_TRACE", "1")
    monkeypatch.setenv("OXENCLAW_LLM_TRACE_FILE", str(sink))
    monkeypatch.setenv("OXENCLAW_LLM_TRACE_MAX_BODY", "1024")

    huge = "x" * 5000
    llm_trace.log_request(
        request_id="r9",
        provider="ollama",
        model_id="qwen3.5:9b",
        url="http://x",
        payload={"messages": [{"role": "user", "content": huge}]},
    )
    line = _read_lines(sink)[0]
    body = line["payload"]["messages"][0]["content"]
    assert body.startswith("x" * 1024)
    assert "[truncated" in body


def test_error_event_carries_status_and_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = tmp_path / "trace.jsonl"
    monkeypatch.setenv("OXENCLAW_LLM_TRACE", "1")
    monkeypatch.setenv("OXENCLAW_LLM_TRACE_FILE", str(sink))

    llm_trace.log_error(
        request_id="r2",
        provider="anthropic",
        model_id="claude-haiku-4-5-20251001",
        status=429,
        message="rate limited",
        duration_ms=88.8,
    )
    line = _read_lines(sink)[0]
    assert line == {
        **line,
        "event": "error",
        "status": 429,
        "message": "rate limited",
        "provider": "anthropic",
    }
