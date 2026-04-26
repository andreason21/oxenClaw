"""Tests for ThreadBindings: bidirectional lookup + idle/max-age eviction."""

from __future__ import annotations

from oxenclaw.extensions.telegram.thread_bindings import ThreadBindings


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_bind_and_lookup_both_directions() -> None:
    b = ThreadBindings(clock=_FakeClock(100.0))
    b.bind(agent_id="echo", session_key="s", chat_id=10, thread_id=20)
    fromsession = b.get_by_session("echo", "s")
    fromthread = b.get_by_thread(10, 20)
    assert fromsession is fromthread
    assert fromsession is not None
    assert fromsession.chat_id == 10
    assert fromsession.thread_id == 20


def test_missing_returns_none() -> None:
    b = ThreadBindings()
    assert b.get_by_session("nobody", "x") is None
    assert b.get_by_thread(1, 2) is None


def test_rebinding_same_session_replaces_thread_map() -> None:
    b = ThreadBindings(clock=_FakeClock(100.0))
    b.bind(agent_id="echo", session_key="s", chat_id=10, thread_id=20)
    b.bind(agent_id="echo", session_key="s", chat_id=10, thread_id=30)
    assert b.get_by_thread(10, 20) is None
    assert b.get_by_thread(10, 30) is not None
    assert len(b) == 1


def test_rebinding_same_thread_replaces_session_map() -> None:
    b = ThreadBindings(clock=_FakeClock(100.0))
    b.bind(agent_id="a", session_key="s1", chat_id=10, thread_id=20)
    b.bind(agent_id="a", session_key="s2", chat_id=10, thread_id=20)
    assert b.get_by_session("a", "s1") is None
    assert b.get_by_session("a", "s2") is not None
    assert len(b) == 1


def test_idle_eviction_on_read() -> None:
    clock = _FakeClock(0.0)
    b = ThreadBindings(idle_seconds=10.0, max_age_seconds=1000.0, clock=clock)
    b.bind(agent_id="a", session_key="s", chat_id=1, thread_id=1)
    clock.now = 11.0
    assert b.get_by_session("a", "s") is None
    assert len(b) == 0


def test_max_age_eviction_on_read() -> None:
    clock = _FakeClock(0.0)
    b = ThreadBindings(idle_seconds=1_000_000.0, max_age_seconds=100.0, clock=clock)
    b.bind(agent_id="a", session_key="s", chat_id=1, thread_id=1)
    # Touch within idle so only max_age can trigger eviction.
    clock.now = 50.0
    assert b.get_by_session("a", "s") is not None
    clock.now = 200.0
    assert b.get_by_session("a", "s") is None


def test_touch_refreshes_idle_timer() -> None:
    clock = _FakeClock(0.0)
    b = ThreadBindings(idle_seconds=10.0, max_age_seconds=1000.0, clock=clock)
    b.bind(agent_id="a", session_key="s", chat_id=1, thread_id=1)
    clock.now = 9.0
    assert b.get_by_session("a", "s") is not None  # refreshes last_used_at
    clock.now = 15.0  # under 10s since last touch
    assert b.get_by_session("a", "s") is not None


def test_prune_drops_all_expired_entries() -> None:
    clock = _FakeClock(0.0)
    b = ThreadBindings(idle_seconds=10.0, max_age_seconds=1000.0, clock=clock)
    b.bind(agent_id="a", session_key="s1", chat_id=1, thread_id=1)
    b.bind(agent_id="a", session_key="s2", chat_id=2, thread_id=2)
    clock.now = 100.0
    assert b.prune() == 2
    assert len(b) == 0


def test_rejects_non_positive_ttls() -> None:
    import pytest

    with pytest.raises(ValueError):
        ThreadBindings(idle_seconds=0.0)
    with pytest.raises(ValueError):
        ThreadBindings(max_age_seconds=0.0)
