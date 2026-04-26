"""Phase 7: system prompt assembly + cache observability."""

from __future__ import annotations

from oxenclaw.pi.cache_observability import (
    CacheObserver,
    should_apply_cache_markers,
)
from oxenclaw.pi.system_prompt import (
    SystemPromptContribution,
    assemble_system_prompt,
    embedded_context_contribution,
    memory_contribution,
    skills_contribution,
    time_contribution,
)

# ─── system prompt assembly ──────────────────────────────────────────


def test_assemble_orders_by_priority_and_skips_disabled() -> None:
    parts = [
        SystemPromptContribution(name="b", body="B body", priority=20),
        SystemPromptContribution(name="a", body="A body", priority=10),
        SystemPromptContribution(name="c", body="", priority=5),  # empty → skip
        SystemPromptContribution(name="d", body="D body", priority=15, enabled=False),
    ]
    out, _prefix = assemble_system_prompt("BASE", parts)
    assert out.startswith("BASE")
    assert out.index("A body") < out.index("B body")
    assert "C body" not in out and "D body" not in out


def test_cacheable_prefix_count_excludes_volatile() -> None:
    parts = [
        skills_contribution(skills_block="skills here"),
        embedded_context_contribution(files_block="docs here"),
        memory_contribution(memory_block="recall here"),  # not cacheable
    ]
    _, prefix_count = assemble_system_prompt("BASE", parts)
    # Two cacheable contributions (skills + embedded). Memory comes after
    # them in priority order (80 vs 20/30) so prefix should be 2.
    assert prefix_count == 2


def test_time_contribution_marked_volatile() -> None:
    t = time_contribution(iso_now="2026-04-25T12:34:56", timezone="KST")
    assert t.cacheable is False
    assert "2026-04-25" in t.body


def test_mode_appends_footer_when_not_chat() -> None:
    out, _ = assemble_system_prompt("BASE", [], mode="code")
    assert out.endswith("[mode:code]")
    out, _ = assemble_system_prompt("BASE", [], mode="chat")
    assert "[mode" not in out


# ─── cache observability ─────────────────────────────────────────────


def test_observer_accumulates_and_computes_hit_rate() -> None:
    obs = CacheObserver()
    obs.record({"input_tokens": 100, "cache_read_input_tokens": 0})
    obs.record(
        {"input_tokens": 0, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0}
    )
    rate = obs.hit_rate()
    # 100 read / (100 read + 100 input) = 0.5
    assert abs(rate - 0.5) < 1e-9
    assert obs.summary()["turns"] == 2


def test_observer_hit_rate_zero_when_no_data() -> None:
    obs = CacheObserver()
    assert obs.hit_rate() == 0.0
    assert obs.cache_alive() is False


def test_should_apply_cache_markers_warmup_then_decide() -> None:
    obs = CacheObserver()
    # First few turns: always warm.
    for _ in range(2):
        obs.record({"input_tokens": 1000, "cache_read_input_tokens": 0})
        assert should_apply_cache_markers(obs) is True
    # Third turn: still in warmup window (min_turns=3).
    obs.record({"input_tokens": 1000, "cache_read_input_tokens": 0})
    # Now we're past min_turns. With no hits and no recent hit, drop markers.
    obs.record({"input_tokens": 1000, "cache_read_input_tokens": 0})
    assert should_apply_cache_markers(obs) is False


def test_should_apply_cache_markers_keeps_when_hit_rate_high() -> None:
    obs = CacheObserver()
    obs.record({"input_tokens": 100, "cache_read_input_tokens": 0})
    obs.record({"input_tokens": 100, "cache_read_input_tokens": 0})
    obs.record({"input_tokens": 100, "cache_read_input_tokens": 9000})
    assert should_apply_cache_markers(obs) is True
