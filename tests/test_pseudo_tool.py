"""Pseudo-tool extraction from assistant reply text."""

from __future__ import annotations

from oxenclaw.agents.pseudo_tool import extract_pseudo_tool_call

KNOWN = {"weather", "web_search", "memory_save", "cron", "skill_run"}


def _is_known(name: str) -> bool:
    return name in KNOWN


# Schemas the schema-shape fallback consults when no name field is present.
TOOL_SCHEMAS = {
    "cron": {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "schedule": {"type": "string"},
            "prompt": {"type": "string"},
            "description": {"type": "string"},
            "agent_id": {"type": "string"},
            "channel": {"type": "string"},
            "account_id": {"type": "string"},
            "chat_id": {"type": "string"},
            "thread_id": {"type": "string"},
            "enabled": {"type": "boolean"},
            "job_id": {"type": "string"},
        },
        "required": ["action"],
    },
    "skill_run": {
        "type": "object",
        "properties": {
            "skill": {"type": "string"},
            "script": {"type": "string"},
            "args": {"type": "array"},
        },
        "required": ["skill"],
    },
    "weather": {
        "type": "object",
        "properties": {"location": {"type": "string"}},
        "required": ["location"],
    },
}


def test_returns_none_for_empty_text() -> None:
    assert extract_pseudo_tool_call("", is_known_tool=_is_known) is None
    assert extract_pseudo_tool_call("   ", is_known_tool=_is_known) is None


def test_returns_none_when_no_json_present() -> None:
    text = "수원 날씨를 확인하겠습니다."
    assert extract_pseudo_tool_call(text, is_known_tool=_is_known) is None


def test_extracts_fenced_json_with_flat_args() -> None:
    """The actual failing transcript shape: fenced ```json``` block
    with `{"tool": "weather", "location": "...", "query": "..."}`."""
    text = (
        "수원 날씨를 확인해드리겠습니다.\n\n"
        "```json\n"
        '{"tool": "weather", "location": "Suwon, South Korea", '
        '"query": "weather"}\n'
        "```\n"
        "수원 지역의 현재 날씨 정보를 확인하겠습니다."
    )
    pseudo = extract_pseudo_tool_call(text, is_known_tool=_is_known)
    assert pseudo is not None
    assert pseudo.name == "weather"
    assert pseudo.args == {"location": "Suwon, South Korea", "query": "weather"}


def test_unfenced_json_object_in_text() -> None:
    """Bare top-level JSON without code fence — also valid."""
    text = 'I will check: {"tool": "web_search", "query": "ai news"} now.'
    pseudo = extract_pseudo_tool_call(text, is_known_tool=_is_known)
    assert pseudo is not None
    assert pseudo.name == "web_search"
    assert pseudo.args == {"query": "ai news"}


def test_openai_style_function_wrapping() -> None:
    """Some models emit `{"function": {"name": ..., "arguments": {...}}}`."""
    text = '```json\n{"function": {"name": "weather", "arguments": {"city": "Seoul"}}}\n```'
    pseudo = extract_pseudo_tool_call(text, is_known_tool=_is_known)
    assert pseudo is not None
    assert pseudo.name == "weather"
    assert pseudo.args == {"city": "Seoul"}


def test_name_with_input_key() -> None:
    text = '```json\n{"name": "weather", "input": {"city": "Suwon"}}\n```'
    pseudo = extract_pseudo_tool_call(text, is_known_tool=_is_known)
    assert pseudo is not None
    assert pseudo.name == "weather"
    assert pseudo.args == {"city": "Suwon"}


def test_arguments_as_stringified_json() -> None:
    """OpenAI streaming dumps `arguments` as a JSON string."""
    text = '```json\n{"name": "web_search", "arguments": "{\\"query\\": "kimchi"}"}\n```'
    # The escaped string isn't valid JSON in our test (odd quotes); use a
    # cleaner version:
    text = '```json\n{"name": "web_search", "arguments": "{\\"query\\": \\"kimchi\\"}"}\n```'
    pseudo = extract_pseudo_tool_call(text, is_known_tool=_is_known)
    assert pseudo is not None
    assert pseudo.name == "web_search"
    assert pseudo.args == {"query": "kimchi"}


def test_skips_unknown_tool_names() -> None:
    """Documentation snippets that mention non-registered tools must
    not be auto-fired (false positive risk)."""
    text = '```json\n{"tool": "imaginary_tool", "x": 1}\n```'
    assert extract_pseudo_tool_call(text, is_known_tool=_is_known) is None


def test_picks_first_recognised_call_when_multiple_blocks() -> None:
    text = (
        "First: ```json\n"
        '{"tool": "imaginary", "x": 1}\n'
        "```\n"
        "Then the real one: ```json\n"
        '{"tool": "weather", "city": "Seoul"}\n'
        "```"
    )
    pseudo = extract_pseudo_tool_call(text, is_known_tool=_is_known)
    assert pseudo is not None
    assert pseudo.name == "weather"


def test_invalid_json_is_silently_skipped() -> None:
    text = "```json\n{tool: weather, broken}\n```"
    assert extract_pseudo_tool_call(text, is_known_tool=_is_known) is None


# ─── schema-shape fallback ────────────────────────────────────────────


def test_schema_shape_matches_cron_args_without_name_field() -> None:
    """The actual cron failure: the model emitted a fenced JSON block
    with action/schedule/prompt/description but no `tool` field, so
    the name-based extractor returned None and the cron job never
    registered. With tool_schemas opt-in the shape now matches."""
    text = (
        "I'll set up this recurring market report task for you.\n"
        "```json\n"
        "{\n"
        '  "action": "add",\n'
        '  "schedule": "50 8 * * *",\n'
        '  "prompt": "매일 아침 8:50분 ... 알려줘",\n'
        '  "description": "Daily market report"\n'
        "}\n"
        "```"
    )
    pseudo = extract_pseudo_tool_call(text, is_known_tool=_is_known, tool_schemas=TOOL_SCHEMAS)
    assert pseudo is not None
    assert pseudo.name == "cron"
    assert pseudo.args["action"] == "add"
    assert pseudo.args["schedule"] == "50 8 * * *"


def test_schema_shape_matches_skill_run_args_without_name_field() -> None:
    """Same bug class for skill_run — the model emitted only the
    arguments earlier in the qwen3.5:9b stock-analysis trace."""
    text = '```json\n{"skill":"stock-analysis","script":"analyze_stock.py","args":["KOSPI"]}\n```'
    pseudo = extract_pseudo_tool_call(text, is_known_tool=_is_known, tool_schemas=TOOL_SCHEMAS)
    assert pseudo is not None
    assert pseudo.name == "skill_run"


def test_schema_shape_skipped_when_opt_out() -> None:
    """Without `tool_schemas`, the legacy behaviour is preserved: a
    name-less JSON object falls through to None."""
    text = '```json\n{"action":"add","schedule":"50 8 * * *","prompt":"x"}\n```'
    assert extract_pseudo_tool_call(text, is_known_tool=_is_known) is None


def test_schema_shape_rejects_extra_unknown_keys() -> None:
    """When `additionalProperties` isn't set, an extra key the schema
    doesn't declare disqualifies the match. Otherwise we'd routinely
    misroute partial blobs."""
    text = '```json\n{"action":"add","schedule":"50 8 * * *","prompt":"x","wibble":"q"}\n```'
    pseudo = extract_pseudo_tool_call(text, is_known_tool=_is_known, tool_schemas=TOOL_SCHEMAS)
    assert pseudo is None


def test_schema_shape_refuses_to_guess_on_ambiguous_match() -> None:
    """If two schemas have the same minimal required field, the
    fallback must refuse rather than auto-fire the wrong tool."""
    schemas = {
        "alpha": {
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x"],
        },
        "beta": {
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x"],
        },
    }
    text = '```json\n{"x":"hello"}\n```'
    pseudo = extract_pseudo_tool_call(
        text,
        is_known_tool=lambda n: n in {"alpha", "beta"},
        tool_schemas=schemas,
    )
    assert pseudo is None


def test_schema_shape_skipped_when_required_unsatisfied() -> None:
    """If the candidate JSON is missing a required field, no match."""
    text = '```json\n{"schedule":"0 9 * * *","prompt":"x"}\n```'  # no `action`
    pseudo = extract_pseudo_tool_call(text, is_known_tool=_is_known, tool_schemas=TOOL_SCHEMAS)
    assert pseudo is None


def test_named_match_still_wins_over_schema_shape() -> None:
    """When a candidate has both `tool: <name>` and matching schema
    shape, the explicit name path takes precedence so the matcher
    doesn't reroute to a different tool just because shape happens
    to fit."""
    text = '```json\n{"tool":"weather","action":"add","schedule":"50 8 * * *","prompt":"x"}\n```'
    pseudo = extract_pseudo_tool_call(text, is_known_tool=_is_known, tool_schemas=TOOL_SCHEMAS)
    # `tool: weather` is the named call. (`weather` schema requires
    # `location`, which is missing — but we only re-shape match when
    # there's no name. The name path treats this as `weather` with
    # extra args, since flat-shape coercion drops nothing.)
    assert pseudo is not None
    assert pseudo.name == "weather"
