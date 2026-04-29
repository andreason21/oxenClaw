"""Per-turn installed-skill suggestion prelude.

Locks the contract: when a user query keyword-matches an installed
skill's description, the runtime prepends a directive naming the
skill + a `skill_run` call shape so small models stop falling back
to "I can't access real-time data" refusals.
"""

from __future__ import annotations

from dataclasses import dataclass

from oxenclaw.agents.skill_suggest import (
    render_skill_suggestion_prelude,
    suggest_skill_for,
)


@dataclass
class FakeSkill:
    slug: str
    name: str
    description: str
    body: str = ""


def _stock_skill() -> FakeSkill:
    return FakeSkill(
        slug="stock-analysis",
        name="stock-analysis",
        description=(
            "Analyze stocks and cryptocurrencies using Yahoo Finance data. "
            "Supports portfolio management, watchlists, dividend analysis, "
            "8-dimension stock scoring, viral trend detection, and rumor "
            "scanning. Use for 주식 / 주가 analysis, portfolio tracking, "
            "earnings reactions, crypto monitoring."
        ),
        body=(
            "## Quick Commands\n\n"
            "```bash\n"
            "uv run {baseDir}/scripts/analyze_stock.py AAPL\n"
            "```\n"
        ),
    )


def _weather_skill() -> FakeSkill:
    return FakeSkill(
        slug="weather-skill",
        name="weather-skill",
        description="Look up local weather and forecast for any city worldwide.",
        body="bash scripts/forecast.sh Seoul\n",
    )


# ─── suggest_skill_for ──────────────────────────────────────────────


def test_korean_stock_query_matches_stock_skill() -> None:
    s = suggest_skill_for("삼성전자 주가 알려줘", [_stock_skill()])
    assert s is not None
    assert s.slug == "stock-analysis"
    assert "주가" in s.matched_terms


def test_english_stock_query_matches_stock_skill() -> None:
    s = suggest_skill_for("show AAPL stock analysis", [_stock_skill()])
    assert s is not None
    assert s.slug == "stock-analysis"
    assert "stock" in s.matched_terms


def test_unrelated_query_returns_none() -> None:
    """Greetings and small-talk shouldn't false-trigger a skill
    suggestion — that just adds noise to the prompt."""
    for q in ("안녕", "hello", "what time is it", "내 형 이름 뭐야"):
        assert suggest_skill_for(q, [_stock_skill(), _weather_skill()]) is None, q


def test_picks_best_among_multiple_skills() -> None:
    """When a query matches two skills, the higher-scored one wins."""
    s = suggest_skill_for(
        "주가 portfolio analysis",
        [_weather_skill(), _stock_skill()],
    )
    assert s is not None
    assert s.slug == "stock-analysis"


def test_no_skills_no_suggestion() -> None:
    assert suggest_skill_for("주가", []) is None


def test_stopwords_alone_dont_suggest() -> None:
    """Common verbs like 'show me' shouldn't match anything."""
    s = suggest_skill_for("show me", [_stock_skill()])
    assert s is None


# ─── render_skill_suggestion_prelude ────────────────────────────────


def test_prelude_names_skill_and_skill_run() -> None:
    s = suggest_skill_for("삼성전자 주가", [_stock_skill()])
    assert s is not None
    out = render_skill_suggestion_prelude(s, "삼성전자 주가")
    assert "stock-analysis" in out
    assert "skill_run" in out
    # Anti-refusal directive present.
    assert "real-time data" in out


def test_prelude_extracts_sample_invocation_from_body() -> None:
    s = suggest_skill_for("AAPL stock analysis", [_stock_skill()])
    assert s is not None
    out = render_skill_suggestion_prelude(s, "AAPL stock analysis")
    # The sample invocation script name pulled from the body.
    assert "analyze_stock.py" in out
    assert "stock-analysis" in out
    # Prose-style spec, not a Python-call template the model can copy verbatim.
    assert "skill_run(" not in out
    assert 'skill="' not in out


def test_prelude_includes_korean_ticker_hint() -> None:
    """The prelude tells the model how to translate Korean tickers
    to Yahoo Finance format — that's the actual gap small models
    have when asked '삼성전자'."""
    s = suggest_skill_for("삼성전자 주가", [_stock_skill()])
    assert s is not None
    out = render_skill_suggestion_prelude(s, "삼성전자 주가")
    assert "005930" in out
    assert ".KS" in out


def test_prelude_falls_back_when_body_has_no_sample() -> None:
    """A skill with no parseable invocation in its body still gets
    a useful prelude — just with a placeholder script."""
    bare = FakeSkill(
        slug="bare", name="bare", description="주가 analysis tool.",
        body="No quick commands documented here.\n",
    )
    s = suggest_skill_for("주가", [bare])
    assert s is not None
    out = render_skill_suggestion_prelude(s, "주가")
    assert "bare" in out
    assert "skill_run" in out
    # No Python-call template — see render_skill_suggestion_prelude docstring.
    assert "skill_run(" not in out
