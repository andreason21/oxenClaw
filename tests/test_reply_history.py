"""Phase R1: reply-history per-thread context buffer."""

from __future__ import annotations

from sampyclaw.channels.reply_history import (
    CURRENT_MESSAGE_MARKER,
    DEFAULT_GROUP_HISTORY_LIMIT,
    HISTORY_CONTEXT_MARKER,
    MAX_HISTORY_KEYS,
    HistoryEntry,
    build_history_context,
    build_history_context_from_entries,
    build_history_context_from_map,
    build_pending_history_context_from_map,
    clear_history_entries,
    clear_history_entries_if_enabled,
    evict_old_history_keys,
    format_entries,
    record_pending_history_entry,
    record_pending_history_entry_if_enabled,
)

# ─── append + limit ─────────────────────────────────────────────────


def test_record_appends_and_caps_per_key_list() -> None:
    m: dict[str, list[HistoryEntry]] = {}
    for i in range(5):
        record_pending_history_entry(m, "k1", HistoryEntry(sender="u", body=f"msg{i}"), limit=3)
    assert [e.body for e in m["k1"]] == ["msg2", "msg3", "msg4"]


def test_record_default_limit_is_50() -> None:
    m: dict[str, list[HistoryEntry]] = {}
    for i in range(60):
        record_pending_history_entry(m, "k", HistoryEntry(sender="u", body=str(i)))
    assert len(m["k"]) == DEFAULT_GROUP_HISTORY_LIMIT


def test_record_if_enabled_handles_none_map() -> None:
    record_pending_history_entry_if_enabled(
        None, "k", HistoryEntry(sender="u", body="hi")
    )  # must not raise


# ─── clear ──────────────────────────────────────────────────────────


def test_clear_removes_bucket() -> None:
    m = {"k1": [HistoryEntry(sender="u", body="x")]}
    clear_history_entries(m, "k1")
    assert "k1" not in m
    # Idempotent.
    clear_history_entries(m, "k1")


def test_clear_if_enabled_handles_none() -> None:
    clear_history_entries_if_enabled(None, "k")


# ─── outer-map LRU eviction ─────────────────────────────────────────


def test_evict_keeps_only_max_keys() -> None:
    m: dict[str, list[HistoryEntry]] = {}
    for i in range(MAX_HISTORY_KEYS + 5):
        m[f"k{i}"] = [HistoryEntry(sender="u", body="x")]
    dropped = evict_old_history_keys(m)
    assert dropped == 5
    assert len(m) == MAX_HISTORY_KEYS
    # Oldest were dropped (insertion order).
    assert "k0" not in m and "k4" not in m
    assert "k5" in m


def test_evict_no_op_below_threshold() -> None:
    m = {"a": [], "b": []}
    dropped = evict_old_history_keys(m, max_keys=10)
    assert dropped == 0


def test_evict_custom_max() -> None:
    m: dict[str, list[HistoryEntry]] = {f"k{i}": [] for i in range(20)}
    dropped = evict_old_history_keys(m, max_keys=5)
    assert dropped == 15
    assert len(m) == 5


# ─── prompt assembly ────────────────────────────────────────────────


def test_format_entries_renders_sender_colon_body() -> None:
    entries = [
        HistoryEntry(sender="alice", body="hi"),
        HistoryEntry(sender="bob", body="hey"),
    ]
    out = format_entries(entries)
    assert out == "alice: hi\nbob: hey"


def test_build_history_context_with_history() -> None:
    out = build_history_context(
        history_text="alice: hi\nbob: hey",
        current_message="@bot what's up?",
    )
    assert HISTORY_CONTEXT_MARKER in out
    assert CURRENT_MESSAGE_MARKER in out
    assert out.index(HISTORY_CONTEXT_MARKER) < out.index(CURRENT_MESSAGE_MARKER)


def test_build_history_context_skips_markers_when_empty() -> None:
    out = build_history_context(history_text="   ", current_message="hi")
    assert out == "hi"
    assert HISTORY_CONTEXT_MARKER not in out


def test_build_from_entries_chains_format_and_wrap() -> None:
    out = build_history_context_from_entries(
        entries=[HistoryEntry(sender="u", body="prior")],
        current_message="now",
    )
    assert "u: prior" in out
    assert "now" in out


def test_build_from_map_uses_key_lookup() -> None:
    m = {"k1": [HistoryEntry(sender="alice", body="hi")]}
    out = build_history_context_from_map(history_map=m, key="k1", current_message="@bot ?")
    assert "alice: hi" in out


def test_build_from_map_missing_key_returns_just_current() -> None:
    out = build_history_context_from_map(history_map={}, key="absent", current_message="hi")
    assert out == "hi"


def test_build_pending_from_none_map_short_circuits() -> None:
    out = build_pending_history_context_from_map(history_map=None, key="any", current_message="hi")
    assert out == "hi"
