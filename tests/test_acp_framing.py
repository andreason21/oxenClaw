"""NDJSON framing round-trip + 4-verb schema validation tests."""

from __future__ import annotations

import io
import json

import pytest
from pydantic import ValidationError

from oxenclaw.acp.framing import (
    AcpFramingError,
    BytesIOReader,
    BytesIOWriter,
    encode_message,
    read_messages,
    write_message,
)
from oxenclaw.acp.protocol import (
    PROTOCOL_VERSION,
    CancelParams,
    InitializeParams,
    InitializeResult,
    JsonRpcError,
    NewSessionParams,
    NewSessionResult,
    PromptContentText,
    PromptParams,
    PromptResult,
    notification_envelope,
    request_envelope,
    response_envelope,
)


# --- framing ---------------------------------------------------------------


async def test_encode_message_appends_single_newline() -> None:
    payload = encode_message({"jsonrpc": "2.0", "method": "ping"})
    assert payload.endswith(b"\n")
    assert payload.count(b"\n") == 1


async def test_encode_message_preserves_unicode() -> None:
    payload = encode_message({"text": "안녕 — ✓"})
    decoded = json.loads(payload.rstrip(b"\n").decode("utf-8"))
    assert decoded["text"] == "안녕 — ✓"


async def test_round_trip_via_bytesio() -> None:
    buf = io.BytesIO()
    sink = BytesIOWriter(buf)
    await write_message(sink, {"jsonrpc": "2.0", "id": 1, "method": "a"})
    await write_message(sink, {"jsonrpc": "2.0", "id": 2, "method": "b"})
    buf.seek(0)
    reader = BytesIOReader(buf)
    out: list[dict[str, object]] = []
    async for msg in read_messages(reader):
        out.append(msg)
    assert len(out) == 2
    assert out[0]["method"] == "a"
    assert out[1]["method"] == "b"


async def test_read_messages_skips_blank_lines() -> None:
    buf = io.BytesIO(b"\n\n" + encode_message({"k": 1}) + b"\n")
    reader = BytesIOReader(buf)
    out: list[dict[str, object]] = []
    async for msg in read_messages(reader):
        out.append(msg)
    assert out == [{"k": 1}]


async def test_read_messages_raises_on_malformed_line() -> None:
    buf = io.BytesIO(b"{not-json}\n")
    reader = BytesIOReader(buf)
    with pytest.raises(AcpFramingError, match="malformed JSON"):
        async for _ in read_messages(reader):
            pass


async def test_read_messages_rejects_top_level_array() -> None:
    buf = io.BytesIO(b"[1,2,3]\n")
    reader = BytesIOReader(buf)
    with pytest.raises(AcpFramingError, match="must be a JSON object"):
        async for _ in read_messages(reader):
            pass


async def test_read_messages_enforces_max_line_bytes() -> None:
    # 1 KB cap, 2 KB line
    big = b"{" + b'"k":"' + (b"x" * 2048) + b'"}' + b"\n"
    buf = io.BytesIO(big)
    reader = BytesIOReader(buf)
    with pytest.raises(AcpFramingError, match="exceeds max_line_bytes"):
        async for _ in read_messages(reader, max_line_bytes=1024):
            pass


# --- protocol --------------------------------------------------------------


def test_initialize_round_trip_via_alias() -> None:
    raw = {
        "protocolVersion": PROTOCOL_VERSION,
        "clientInfo": {"name": "zed", "version": "0.1"},
    }
    params = InitializeParams.model_validate(raw)
    assert params.protocol_version == PROTOCOL_VERSION
    assert params.client_info == {"name": "zed", "version": "0.1"}
    assert params.model_dump(by_alias=True, exclude_none=True) == raw


def test_initialize_result_carries_agent_info() -> None:
    result = InitializeResult.model_validate(
        {
            "protocolVersion": PROTOCOL_VERSION,
            "agentInfo": {"name": "oxenclaw", "version": "0.0.0"},
        }
    )
    dumped = result.model_dump(by_alias=True, exclude_none=True)
    assert dumped["agentInfo"]["name"] == "oxenclaw"
    assert dumped["protocolVersion"] == PROTOCOL_VERSION


def test_new_session_meta_passes_through_unmodelled_keys() -> None:
    # `_meta.sessionKey` is openclaw-specific; pydantic must allow
    # arbitrary keys inside `meta` without modelling each one.
    raw = {
        "cwd": "/tmp",
        "_meta": {"sessionKey": "agent:42", "resetSession": True},
    }
    params = NewSessionParams.model_validate(raw)
    assert params.meta == {"sessionKey": "agent:42", "resetSession": True}
    assert params.model_dump(by_alias=True, exclude_none=True) == raw


def test_new_session_result_round_trip() -> None:
    raw = {"sessionId": "sess-abc"}
    parsed = NewSessionResult.model_validate(raw)
    assert parsed.session_id == "sess-abc"
    assert parsed.model_dump(by_alias=True, exclude_none=True) == raw


def test_prompt_params_with_text_content() -> None:
    raw = {
        "sessionId": "sess-1",
        "prompt": [{"type": "text", "text": "hello"}],
    }
    parsed = PromptParams.model_validate(raw)
    assert parsed.session_id == "sess-1"
    block = parsed.prompt[0]
    assert isinstance(block, PromptContentText)
    assert block.text == "hello"


def test_prompt_content_discriminator_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        PromptParams.model_validate(
            {
                "sessionId": "s",
                "prompt": [{"type": "audio", "data": "..."}],
            }
        )


def test_prompt_result_stop_reason_enum() -> None:
    PromptResult.model_validate({"stopReason": "stop"})
    PromptResult.model_validate({"stopReason": "cancel"})
    PromptResult.model_validate({"stopReason": "error"})
    with pytest.raises(ValidationError):
        PromptResult.model_validate({"stopReason": "explode"})


def test_cancel_params_minimal() -> None:
    parsed = CancelParams.model_validate({"sessionId": "s1"})
    assert parsed.session_id == "s1"


def test_request_envelope_serialises_pydantic_params() -> None:
    env = request_envelope(
        id=1,
        method="initialize",
        params=InitializeParams(
            protocolVersion=PROTOCOL_VERSION  # type: ignore[call-arg]
        ),
    )
    assert env == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": PROTOCOL_VERSION},
    }


def test_notification_envelope_omits_id_field() -> None:
    env = notification_envelope(
        method="session/update",
        params={"sessionId": "s", "update": {"sessionUpdate": "agent_message_chunk"}},
    )
    assert "id" not in env
    assert env["method"] == "session/update"


def test_response_envelope_requires_exactly_one_of_result_error() -> None:
    with pytest.raises(ValueError):
        response_envelope(id=1)
    with pytest.raises(ValueError):
        response_envelope(
            id=1,
            result={"x": 1},
            error=JsonRpcError(code=-32600, message="bad"),
        )
    ok = response_envelope(id=1, result=PromptResult(stopReason="stop"))  # type: ignore[call-arg]
    assert ok == {"jsonrpc": "2.0", "id": 1, "result": {"stopReason": "stop"}}


# --- end-to-end: framing + protocol stitched ------------------------------


async def test_request_round_trip_through_ndjson() -> None:
    """The "smoke test" the commit message promised: build a request,
    write it as NDJSON, read it back, and validate via pydantic."""
    buf = io.BytesIO()
    sink = BytesIOWriter(buf)
    sent = request_envelope(
        id=1,
        method="initialize",
        params=InitializeParams(
            protocolVersion=PROTOCOL_VERSION,  # type: ignore[call-arg]
            clientInfo={"name": "test"},  # type: ignore[call-arg]
        ),
    )
    await write_message(sink, sent)

    buf.seek(0)
    reader = BytesIOReader(buf)
    received: list[dict[str, object]] = []
    async for m in read_messages(reader):
        received.append(m)
    assert len(received) == 1
    msg = received[0]
    assert msg["method"] == "initialize"
    parsed = InitializeParams.model_validate(msg["params"])
    assert parsed.protocol_version == PROTOCOL_VERSION
    assert parsed.client_info == {"name": "test"}
