"""ACP (Agent Client Protocol) wire layer for oxenclaw.

Mirrors openclaw's `src/acp/` package. The TypeScript side leans on
`@agentclientprotocol/sdk` (0.19.x) for framing + schema; on the
Python side we own both because there is no first-party Python SDK.

Submodules:

  - `framing`   — NDJSON reader/writer over an `asyncio.StreamReader`/
                  `StreamWriter` pair (or any byte-level transport).
  - `protocol`  — pydantic models for the four foundational verbs
                  (`initialize`, `newSession`, `prompt`, `cancel`)
                  plus the `session/update` notification envelope.

This package contains no I/O of its own — it is the wire layer.
Process orchestration lives in `oxenclaw.agents.acp_subprocess` and
the future `oxenclaw.agents.acp_runtime` backends.
"""

from __future__ import annotations
