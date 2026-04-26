"""Tests for ConversationHistory: atomic save + roundtrip."""

from __future__ import annotations

from oxenclaw.agents.history import ConversationHistory


def test_empty_history_when_file_missing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = ConversationHistory(tmp_path / "missing.json")
    assert h.messages() == []
    assert len(h) == 0


def test_append_and_save_roundtrip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "nested" / "s.json"
    h = ConversationHistory(path)
    h.append({"role": "user", "content": "hi"})
    h.append({"role": "assistant", "content": [{"type": "text", "text": "hello"}]})
    h.save()

    reloaded = ConversationHistory(path)
    assert len(reloaded) == 2
    assert reloaded.messages()[0]["role"] == "user"
    assert reloaded.messages()[1]["content"][0]["text"] == "hello"


def test_messages_returns_copy(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = ConversationHistory(tmp_path / "s.json")
    h.append({"role": "user", "content": "hi"})
    got = h.messages()
    got.append({"role": "assistant", "content": "mutated"})
    assert len(h) == 1  # internal state not affected


def test_extend(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = ConversationHistory(tmp_path / "s.json")
    h.extend(
        [
            {"role": "user", "content": "a"},
            {"role": "user", "content": "b"},
        ]
    )
    assert len(h) == 2


def test_clear(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = ConversationHistory(tmp_path / "s.json")
    h.append({"role": "user", "content": "x"})
    h.clear()
    assert h.messages() == []


def test_save_is_atomic(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Tempfile should not linger after save completes."""
    path = tmp_path / "s.json"
    h = ConversationHistory(path)
    h.append({"role": "user", "content": "x"})
    h.save()
    assert path.exists()
    assert not any(p.suffix == ".tmp" for p in tmp_path.iterdir())


def test_non_ascii_preserved(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "s.json"
    h = ConversationHistory(path)
    h.append({"role": "user", "content": "안녕하세요 🦞"})
    h.save()
    reloaded = ConversationHistory(path)
    assert reloaded.messages()[0]["content"] == "안녕하세요 🦞"


def test_load_corrupt_json_falls_back_to_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    h = ConversationHistory(path)
    assert h.messages() == []


def test_truncate_preserves_system_and_drops_oldest(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = ConversationHistory(tmp_path / "t.json")
    h.append({"role": "system", "content": "S" * 500})
    for i in range(20):
        h.append({"role": "user", "content": f"u{i}-" + "x" * 200})
        h.append({"role": "assistant", "content": f"a{i}-" + "y" * 200})
    dropped = h.truncate_to_window(max_chars=2_000)
    assert dropped > 0
    msgs = h.messages()
    assert msgs[0]["role"] == "system"
    # Window holds at least the most recent exchange.
    assert msgs[-1]["role"] == "assistant"
    # Total serialized size below cap (with small slack for the system msg).
    import json as _json

    total = sum(len(_json.dumps(m, ensure_ascii=False)) for m in msgs)
    assert total <= 2_000 or len(msgs) == 2  # system + last turn floor


def test_truncate_drops_tool_results_with_their_assistant(tmp_path) -> None:  # type: ignore[no-untyped-def]
    h = ConversationHistory(tmp_path / "t.json")
    h.append({"role": "system", "content": "S"})
    # Old assistant + tool result that should drop together.
    h.append({"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]})
    h.append({"role": "tool", "tool_call_id": "1", "content": "X" * 5000})
    h.append({"role": "user", "content": "fresh"})
    h.truncate_to_window(max_chars=200)
    roles = [m["role"] for m in h.messages()]
    # No orphan tool result left behind.
    assert "tool" not in roles or roles.index("tool") > roles.index("assistant")
