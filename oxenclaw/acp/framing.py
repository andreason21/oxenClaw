"""NDJSON framing for ACP wire I/O.

ACP rides on newline-delimited JSON over stdio (or any byte stream).
The TS reference uses `AgentSideConnection.ndJsonStream` from the
official SDK; here we own the framing because there is no
first-party Python SDK.

Layout (per docs.acp.md):

    one JSON value per line, terminated by '\n'
    UTF-8 encoded
    no leading whitespace, no trailing comments

Two failure modes the framer treats as fatal vs recoverable:

  - **Truncated read** (peer closed mid-line): yield clean EOF.
  - **Malformed JSON line**: raise `AcpFramingError` so the peer
    can be sent an `error` response and the session torn down.

The framer is transport-agnostic — it accepts an
`asyncio.StreamReader` or any object with `readline()` returning a
`bytes`-yielding awaitable. For tests, wrap a `BytesIO` via
`bytes_reader_to_stream`.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator
from typing import Any, Protocol


class AcpFramingError(Exception):
    """Raised when a line cannot be parsed as a JSON value."""


class _BytesReader(Protocol):
    async def readline(self) -> bytes: ...


async def read_messages(
    reader: _BytesReader,
    *,
    max_line_bytes: int = 4 * 1024 * 1024,
) -> AsyncIterator[dict[str, Any]]:
    """Yield decoded JSON objects, one per NDJSON line, until EOF.

    Empty lines (just `\n` or whitespace) are ignored — some peers
    pad their streams. UTF-8 multibyte boundaries are safe because
    `readline()` operates on full lines.

    `max_line_bytes` guards against a peer that never emits `\n`.
    Lines longer than the cap raise `AcpFramingError`.
    """
    while True:
        line = await reader.readline()
        if not line:
            # EOF — clean stream close.
            return
        if len(line) > max_line_bytes:
            raise AcpFramingError(
                f"line exceeds max_line_bytes ({len(line)} > {max_line_bytes})"
            )
        stripped = line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise AcpFramingError(
                f"malformed JSON on wire: {exc.msg} at pos {exc.pos}"
            ) from exc
        if not isinstance(value, dict):
            raise AcpFramingError(
                f"top-level value must be a JSON object, got {type(value).__name__}"
            )
        yield value


def encode_message(message: dict[str, Any]) -> bytes:
    """Encode a single JSON-RPC envelope to NDJSON bytes.

    `ensure_ascii=False` keeps multibyte UTF-8 intact on the wire
    (important for non-ASCII text in `prompt` content). Trailing
    `\n` is mandatory — JSON bodies must not contain literal
    newlines so this is unambiguous.
    """
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


async def write_message(
    writer: asyncio.StreamWriter | _AsyncByteSink, message: dict[str, Any]
) -> None:
    """Write a single message and flush the underlying transport.

    `flush` semantics: callers that batch multiple writes can skip
    the per-message drain by writing raw `encode_message` output
    directly. This helper is the safe default.
    """
    payload = encode_message(message)
    if isinstance(writer, asyncio.StreamWriter):
        writer.write(payload)
        await writer.drain()
        return
    await writer.write(payload)


class _AsyncByteSink(Protocol):
    async def write(self, data: bytes) -> None: ...


class BytesIOReader:
    """Test-only async adapter over a sync `io.BytesIO`.

    Implements just enough of `asyncio.StreamReader` for `readline`
    to satisfy `read_messages`. Don't use this in production — real
    transports must come from `asyncio.open_connection` /
    `asyncio.subprocess`.
    """

    def __init__(self, buf: io.BytesIO) -> None:
        self._buf = buf

    async def readline(self) -> bytes:
        return self._buf.readline()


class BytesIOWriter:
    """Test-only async adapter over a sync `io.BytesIO`."""

    def __init__(self, buf: io.BytesIO) -> None:
        self._buf = buf

    async def write(self, data: bytes) -> None:
        self._buf.write(data)


__all__ = [
    "AcpFramingError",
    "BytesIOReader",
    "BytesIOWriter",
    "encode_message",
    "read_messages",
    "write_message",
]
