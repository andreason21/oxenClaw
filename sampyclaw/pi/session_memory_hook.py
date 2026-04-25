"""Session memory hook.

Mirrors `openclaw/src/hooks/bundled/session-memory/handler.ts` +
`transcript.ts`. When a session is archived (or explicitly indexed), the
hook:

1. Renders the transcript to a markdown-formatted string.
2. Chunks via `memory.chunker.chunk_markdown`.
3. Embeds via `EmbeddingCache` (shared with the rest of the indexer).
4. Inserts under a synthetic path `sessions/<agent_id>/<session_id>.md`
   with `source="sessions"` so `MemoryRetriever.search()` surfaces it
   alongside markdown-from-disk memory.

Wire-up: PiAgent / Dispatcher subscribes the hook to a `LifecycleBus` so
`session.archived` (or your custom `session.completed`) triggers indexing.
The hook is idempotent — re-indexing the same transcript replaces prior
chunks for that path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from sampyclaw.memory.chunker import chunk_markdown
from sampyclaw.memory.embedding_cache import EmbeddingCache
from sampyclaw.memory.hashing import sha256_text
from sampyclaw.memory.store import MemoryStore
from sampyclaw.pi.lifecycle import LifecycleBus, LifecycleEvent
from sampyclaw.pi.messages import (
    AgentMessage,
    AssistantMessage,
    SystemMessage,
    TextContent,
    ThinkingBlock,
    ToolResultBlock,
    ToolResultMessage,
    ToolUseBlock,
    UserMessage,
)
from sampyclaw.pi.session import AgentSession, SessionManager
from sampyclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("pi.session_memory")


SESSION_SOURCE = "sessions"


def _render_block(block: Any) -> str:
    if isinstance(block, TextContent):
        return block.text
    if isinstance(block, ToolUseBlock):
        return f"[tool_use {block.name}({block.input})]"
    if isinstance(block, ThinkingBlock):
        # Thinking is internal reasoning — keep but mark so retrieval
        # ranking can downweight if needed.
        return f"[thinking]\n{block.thinking}\n[/thinking]"
    if isinstance(block, ToolResultBlock):
        body = block.content if isinstance(block.content, str) else str(block.content)
        marker = " (error)" if block.is_error else ""
        return f"[tool_result{marker} for {block.tool_use_id}]\n{body}"
    return str(block)


def render_transcript(session: AgentSession) -> str:
    """Markdown rendering used as the input to the chunker.

    Format:
        # Session <id> — <title>
        Agent: <agent_id>  Model: <model_id>
        ## turn 1 — user
        <text>
        ## turn 1 — assistant
        <text>
        ## tool_results
        ...
    """
    lines: list[str] = []
    title = session.title or session.id[:8]
    lines.append(f"# Session {session.id} — {title}")
    lines.append(
        f"Agent: {session.agent_id}  Model: {session.model_id or '(unset)'}"
    )
    lines.append("")
    turn_idx = 0
    for msg in session.messages:
        if isinstance(msg, SystemMessage):
            lines.append("## system")
            lines.append(msg.content)
            lines.append("")
        elif isinstance(msg, UserMessage):
            turn_idx += 1
            lines.append(f"## turn {turn_idx} — user")
            if isinstance(msg.content, str):
                lines.append(msg.content)
            else:
                for b in msg.content:
                    lines.append(_render_block(b))
            lines.append("")
        elif isinstance(msg, AssistantMessage):
            lines.append(f"## turn {turn_idx} — assistant")
            for b in msg.content:
                lines.append(_render_block(b))
            lines.append("")
        elif isinstance(msg, ToolResultMessage):
            lines.append(f"## turn {turn_idx} — tool_results")
            for r in msg.results:
                lines.append(_render_block(r))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def session_memory_path(session: AgentSession) -> str:
    """Synthetic path used as the MemoryStore primary key for this session."""
    return f"sessions/{session.agent_id}/{session.id}.md"


@dataclass
class SessionMemoryHook:
    """Indexes session transcripts into MemoryStore on demand or via events.

    `min_messages` and `min_chars` skip trivial sessions so the memory
    isn't polluted with one-line greetings.
    """

    store: MemoryStore
    embeddings: EmbeddingCache
    min_messages: int = 4
    min_chars: int = 200
    max_chars: int = 2000  # passed to chunker

    async def index_session(self, session: AgentSession) -> int:
        """Index `session` into MemoryStore. Returns the chunk count written.

        Returns 0 (and skips) if the session is below the size thresholds.
        Idempotent: replaces existing chunks for this session's path.
        """
        if len(session.messages) < self.min_messages:
            logger.debug(
                "skip session %s: only %d messages", session.id, len(session.messages)
            )
            return 0
        text = render_transcript(session)
        if len(text) < self.min_chars:
            logger.debug("skip session %s: transcript too short", session.id)
            return 0

        path = session_memory_path(session)
        chunks = chunk_markdown(text, max_chars=self.max_chars)
        if not chunks:
            return 0

        # Embed bodies in one batch through the shared cache.
        bodies = [body for _, _, body in chunks]
        vectors = await self.embeddings.embed(bodies)

        # Stamp file metadata.
        self.store.upsert_file(
            path=path,
            source=SESSION_SOURCE,
            hash_=sha256_text(text),
            mtime=time.time(),
            size=len(text.encode("utf-8")),
        )
        # Atomic replace.
        rows = []
        for (start, end, body), vec in zip(chunks, vectors):
            rows.append((start, end, body, sha256_text(body), vec))
        self.store.replace_chunks_for_file(
            path=path,
            source=SESSION_SOURCE,
            model=self.embeddings.model,
            chunks=rows,
        )
        logger.info(
            "indexed session %s (%d chunks, %d bytes)",
            session.id,
            len(rows),
            len(text),
        )
        return len(rows)

    async def remove_session(self, session_id: str, agent_id: str) -> None:
        path = f"sessions/{agent_id}/{session_id}.md"
        self.store.delete_file(path)

    # ─── LifecycleBus integration ──────────────────────────────────

    def attach(
        self,
        bus: LifecycleBus,
        sessions: SessionManager,
        *,
        on_archived: bool = True,
        on_reset: bool = False,
    ) -> None:
        """Subscribe this hook to a LifecycleBus.

        - `on_archived=True`: index whenever a session is archived (the
          common end-of-life signal).
        - `on_reset=True`: index BEFORE a reset wipes messages so the
          history isn't lost. The bus emits AFTER the reset though, so
          the hook would see an empty session — leave False unless your
          flow emits a pre-reset event.
        """

        async def _on_event(kind: LifecycleEvent, payload: dict) -> None:  # type: ignore[type-arg]
            sid = payload.get("id") or payload.get("source_id")
            if not isinstance(sid, str):
                return
            if kind is LifecycleEvent.ARCHIVED and on_archived:
                # The session may already be deleted from the store; load
                # from the snapshot we have via the payload's "archive_path"
                # if present. Otherwise try to fetch — may return None.
                live = await sessions.get(sid)
                if live is not None:
                    await self.index_session(live)
            elif kind is LifecycleEvent.RESET and on_reset:
                live = await sessions.get(sid)
                if live is not None:
                    await self.index_session(live)
            elif kind is LifecycleEvent.DELETED:
                agent_id = payload.get("agent_id", "")
                if agent_id:
                    await self.remove_session(sid, agent_id)

        bus.subscribe(_on_event)


__all__ = [
    "SESSION_SOURCE",
    "SessionMemoryHook",
    "render_transcript",
    "session_memory_path",
]
