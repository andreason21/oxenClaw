"""Forum-topic ↔ agent-conversation binding.

Telegram supergroups with `is_forum=True` have persistent topic threads. openclaw
maps each `(agent_id, session_key)` conversation to a Telegram topic so replies
land in the right thread; inbound topic messages resolve back to the same
session so multi-turn context is preserved.

This is the in-memory port; persistence to `~/.oxenclaw/agents/<id>/` lands
in a later phase. Port of `extensions/telegram/src/thread-bindings.ts`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class ThreadBinding:
    agent_id: str
    session_key: str
    chat_id: int
    thread_id: int
    created_at: float
    last_used_at: float


class ThreadBindings:
    """Bi-directional (agent, session) ↔ (chat, thread) map with idle + max-age eviction."""

    def __init__(
        self,
        *,
        idle_seconds: float = 3600.0,
        max_age_seconds: float = 86400.0,
        clock: callable = time.time,  # type: ignore[type-arg]
    ) -> None:
        if idle_seconds <= 0 or max_age_seconds <= 0:
            raise ValueError("ttls must be positive")
        self._by_session: dict[tuple[str, str], ThreadBinding] = {}
        self._by_thread: dict[tuple[int, int], ThreadBinding] = {}
        self._idle = idle_seconds
        self._max_age = max_age_seconds
        self._now = clock

    def bind(
        self,
        *,
        agent_id: str,
        session_key: str,
        chat_id: int,
        thread_id: int,
    ) -> ThreadBinding:
        """Create (or replace) a binding. Overwrites any existing entry for the session."""
        # Evict any previous entry on either side to keep the maps consistent.
        previous = self._by_session.pop((agent_id, session_key), None)
        if previous is not None:
            self._by_thread.pop((previous.chat_id, previous.thread_id), None)
        thread_previous = self._by_thread.pop((chat_id, thread_id), None)
        if thread_previous is not None:
            self._by_session.pop((thread_previous.agent_id, thread_previous.session_key), None)

        now = self._now()
        binding = ThreadBinding(
            agent_id=agent_id,
            session_key=session_key,
            chat_id=chat_id,
            thread_id=thread_id,
            created_at=now,
            last_used_at=now,
        )
        self._by_session[(agent_id, session_key)] = binding
        self._by_thread[(chat_id, thread_id)] = binding
        return binding

    def get_by_session(self, agent_id: str, session_key: str) -> ThreadBinding | None:
        return self._resolve(self._by_session.get((agent_id, session_key)))

    def get_by_thread(self, chat_id: int, thread_id: int) -> ThreadBinding | None:
        return self._resolve(self._by_thread.get((chat_id, thread_id)))

    def prune(self) -> int:
        """Drop every expired binding. Returns how many were removed."""
        victims = [b for b in list(self._by_session.values()) if self._expired(b)]
        for b in victims:
            self._remove(b)
        return len(victims)

    def __len__(self) -> int:
        return len(self._by_session)

    def _resolve(self, binding: ThreadBinding | None) -> ThreadBinding | None:
        if binding is None:
            return None
        if self._expired(binding):
            self._remove(binding)
            return None
        binding.last_used_at = self._now()
        return binding

    def _expired(self, b: ThreadBinding) -> bool:
        now = self._now()
        return (now - b.created_at) > self._max_age or (now - b.last_used_at) > self._idle

    def _remove(self, b: ThreadBinding) -> None:
        self._by_session.pop((b.agent_id, b.session_key), None)
        self._by_thread.pop((b.chat_id, b.thread_id), None)
