"""JSON-repair: best-effort fixes for sloppy model JSON."""

from __future__ import annotations

from oxenclaw.pi.run.json_repair import repair_and_parse


def test_clean_json_round_trips_without_repair() -> None:
    parsed, repair = repair_and_parse('{"a": 1}')
    assert parsed == {"a": 1}
    assert repair == ""


def test_trailing_comma_repaired() -> None:
    parsed, repair = repair_and_parse('{"a": 1,}')
    assert parsed == {"a": 1}
    assert repair == "regex-cleanup"


def test_trailing_comma_in_nested_array() -> None:
    parsed, repair = repair_and_parse('{"items": [1, 2, 3,]}')
    assert parsed == {"items": [1, 2, 3]}
    assert repair


def test_smart_quotes_repaired() -> None:
    parsed, repair = repair_and_parse("{“name”: “Suwon”}")
    assert parsed == {"name": "Suwon"}
    assert repair


def test_code_fence_stripped() -> None:
    parsed, repair = repair_and_parse('```json\n{"a": 1}\n```')
    assert parsed == {"a": 1}
    assert repair == "code-fence"


def test_single_quotes_repaired() -> None:
    parsed, repair = repair_and_parse("{'a': 'hello'}")
    assert parsed == {"a": "hello"}
    assert repair == "single-quotes"


def test_missing_closing_brace_repaired() -> None:
    parsed, repair = repair_and_parse('{"a": 1, "b": 2')
    assert parsed == {"a": 1, "b": 2}
    assert repair == "balance-braces"


def test_total_garbage_returns_none() -> None:
    parsed, repair = repair_and_parse("not json at all <<<")
    assert parsed is None
    assert repair == ""


def test_empty_string_returns_none() -> None:
    parsed, repair = repair_and_parse("")
    assert parsed is None
    assert repair == ""
