"""Phase 8: tool runtime hardening tests."""

from __future__ import annotations

from sampyclaw.pi.tool_runtime import (
    DEFAULT_MAX_TOOL_RESULT_CHARS,
    EffectiveToolPolicy,
    MIN_TOOL_RESULT_CHARS,
    ToolContextGuardState,
    ToolNameAllowlist,
    ToolOverride,
    apply_context_guard,
    estimate_tool_result_chars,
    split_large_payload,
    truncate_tool_result,
)


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = ""
        self.input_schema = {"type": "object"}

    async def execute(self, args):  # type: ignore[no-untyped-def]
        return "ok"


# ─── truncation + estimation ────────────────────────────────────────


def test_truncate_short_string_unchanged() -> None:
    out, was_trunc = truncate_tool_result("hello", max_chars=10)
    assert out == "hello"
    assert was_trunc is False


def test_truncate_long_string_keeps_head_and_appends_sentinel() -> None:
    out, was_trunc = truncate_tool_result("x" * 1000, max_chars=200)
    assert was_trunc is True
    assert out.endswith("chars]")
    assert len(out) <= 200


def test_estimate_chars_handles_str_dict_list() -> None:
    assert estimate_tool_result_chars("hi") == 2
    n = estimate_tool_result_chars({"a": 1, "b": [1, 2, 3]})
    assert n > 0
    n2 = estimate_tool_result_chars([1, 2, 3])
    assert n2 > 0


# ─── context guard ──────────────────────────────────────────────────


def test_context_guard_halves_under_pressure() -> None:
    state = ToolContextGuardState()
    initial = state.current_max_chars
    new = apply_context_guard(
        state, used_tokens=80_000, model_context_tokens=100_000
    )
    assert new == initial // 2
    assert state.consecutive_pressure_turns == 1


def test_context_guard_floor_at_min() -> None:
    state = ToolContextGuardState(current_max_chars=MIN_TOOL_RESULT_CHARS)
    new = apply_context_guard(
        state, used_tokens=99_000, model_context_tokens=100_000
    )
    assert new == MIN_TOOL_RESULT_CHARS


def test_context_guard_grows_back_when_relieved() -> None:
    state = ToolContextGuardState(current_max_chars=2_048)
    new = apply_context_guard(
        state, used_tokens=10_000, model_context_tokens=100_000
    )
    assert new == min(DEFAULT_MAX_TOOL_RESULT_CHARS, 4_096)
    assert state.consecutive_pressure_turns == 0


# ─── allowlist ──────────────────────────────────────────────────────


def test_allowlist_empty_allow_means_allow_all() -> None:
    al = ToolNameAllowlist()
    assert al.is_allowed("anything")
    assert al.is_allowed("x.y.z")


def test_allowlist_deny_takes_precedence() -> None:
    al = ToolNameAllowlist(allow=("safe_*",), deny=("safe_dangerous",))
    assert al.is_allowed("safe_read")
    assert not al.is_allowed("safe_dangerous")
    assert not al.is_allowed("evil")


def test_allowlist_filter_drops_disallowed_tools() -> None:
    al = ToolNameAllowlist(allow=("read_*",))
    tools = [_FakeTool("read_file"), _FakeTool("write_file"), _FakeTool("read_url")]
    out = al.filter(tools)
    assert [t.name for t in out] == ["read_file", "read_url"]


# ─── effective policy ───────────────────────────────────────────────


def test_effective_policy_disables_overridden_tool() -> None:
    pol = EffectiveToolPolicy(
        overrides=(ToolOverride(name="dangerous", enabled=False),),
    )
    out = pol.resolve([_FakeTool("safe"), _FakeTool("dangerous")])
    assert [t.name for t in out] == ["safe"]


def test_effective_policy_max_chars_override_wins() -> None:
    pol = EffectiveToolPolicy(
        default_max_result_chars=10_000,
        overrides=(ToolOverride(name="huge", max_result_chars=500),),
    )
    assert pol.max_chars_for("huge") == 500
    assert pol.max_chars_for("ordinary") == 10_000


# ─── splitter ───────────────────────────────────────────────────────


def test_split_large_payload_paginates_lines() -> None:
    items = [f"line-{i}" for i in range(50)]
    pages = split_large_payload(items, page_chars=40, serializer="lines")
    assert len(pages) > 1
    assert pages[0].page == 1
    assert pages[-1].has_more is False
    # Concatenating pages reconstructs (modulo separators).
    full = "\n".join(p.content for p in pages)
    for i in range(50):
        assert f"line-{i}" in full


def test_split_large_payload_json_wraps_arrays() -> None:
    items = [{"i": i} for i in range(10)]
    pages = split_large_payload(items, page_chars=40, serializer="json")
    for p in pages:
        assert p.content.startswith("[") and p.content.endswith("]")


def test_split_empty_payload_returns_single_blank_page() -> None:
    pages = split_large_payload([], page_chars=100)
    assert len(pages) == 1
    assert pages[0].content == ""
    assert pages[0].has_more is False
