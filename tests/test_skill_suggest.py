"""Per-turn installed-skill suggestion prelude.

Locks the contract: when a user query keyword-matches an installed
skill's description, the runtime prepends a directive naming the
skill + a `skill_run` call shape so small models stop falling back
to "I can't access real-time data" refusals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from oxenclaw.agents.skill_suggest import (
    _is_knowledge_style,
    render_skill_suggestion_prelude,
    suggest_skill_for,
)


@dataclass
class FakeSkill:
    slug: str
    name: str
    description: str
    body: str = ""
    skill_md_path: Path | None = field(default=None)


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
            "## Quick Commands\n\n```bash\nuv run {baseDir}/scripts/analyze_stock.py AAPL\n```\n"
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
        slug="bare",
        name="bare",
        description="주가 analysis tool.",
        body="No quick commands documented here.\n"
        "```bash\nuv run scripts/foo.py\n```\n",  # forces script-style detection
    )
    s = suggest_skill_for("주가", [bare])
    assert s is not None
    out = render_skill_suggestion_prelude(s, "주가")
    assert "bare" in out
    assert "skill_run" in out
    # No Python-call template — see render_skill_suggestion_prelude docstring.
    assert "skill_run(" not in out


# ─── knowledge-style branch ─────────────────────────────────────────


def _yahoo_finance_skill(skill_md_path: Path | None = None) -> FakeSkill:
    """yahoo-finance-cli shape: SKILL.md only, documents `yf` CLI, no
    `scripts/` directory. The Korean trigger '주가' is in the description
    via the `_DOMAIN_SYNONYMS` expansion, not the description text."""
    return FakeSkill(
        slug="yahoo-finance-cli",
        name="yahoo-finance",
        description=(
            "Get real-time stock prices, quotes, earnings, and trending "
            "symbols from Yahoo Finance via the `yf` CLI."
        ),
        body=(
            "## Usage\n\n```bash\nyf <module> <symbol>\n```\n"
            "### Quote\n\n```bash\nyf quote AAPL\n```\n"
        ),
        skill_md_path=skill_md_path,
    )


def test_is_knowledge_style_via_filesystem(tmp_path: Path) -> None:
    """Real `InstalledSkill` path — `scripts/` absent → knowledge-style."""
    skill_dir = tmp_path / "yahoo-finance-cli"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("---\nname: x\ndescription: x\n---\n# body\n")
    sk = _yahoo_finance_skill(skill_md_path=skill_md)
    assert _is_knowledge_style(sk, sk.body) is True


def test_is_knowledge_style_false_when_scripts_dir_has_files(tmp_path: Path) -> None:
    skill_dir = tmp_path / "stock-analysis"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "go.py").write_text("print('hi')\n")
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("---\nname: x\ndescription: x\n---\n# body\n")
    sk = FakeSkill(
        slug="stock-analysis",
        name="stock-analysis",
        description="x",
        body="x",
        skill_md_path=skill_md,
    )
    assert _is_knowledge_style(sk, sk.body) is False


def test_is_knowledge_style_body_heuristic_when_no_path() -> None:
    """No `skill_md_path` available (lightweight fakes) → fall back to
    body heuristic. A body that has bash blocks but no `scripts/<file>`
    references is treated as knowledge-style."""
    knowledge = _yahoo_finance_skill()  # no skill_md_path
    assert _is_knowledge_style(knowledge, knowledge.body) is True
    script_like = FakeSkill(
        slug="x", name="x", description="x",
        body="```bash\nuv run scripts/foo.py\n```\n",
    )
    assert _is_knowledge_style(script_like, script_like.body) is False


def test_prelude_for_knowledge_skill_names_shell_tool_explicitly() -> None:
    """The fix: yahoo-finance-cli must NOT be routed via skill_run, and
    the prelude must name the actual registered tool (`shell`) plus its
    argument key (`command`) instead of saying "bash" — small models
    looked for a literal `bash` tool, didn't find one, and fell into a
    `list_dir` enumeration loop instead.

    Locks both directives:
      1. Forbid `skill_run` for this skill.
      2. Name `shell` + `command` exactly so the model can issue the
         call without guessing or exploring."""
    sk = _yahoo_finance_skill()
    s = suggest_skill_for("삼성전자 주가", [sk])
    assert s is not None and s.slug == "yahoo-finance-cli"
    out = render_skill_suggestion_prelude(
        s, "삼성전자 주가", available_tool_names={"shell", "echo", "list_dir"}
    )
    assert "yahoo-finance-cli" in out
    # Names the actual registered tool + its arg key.
    assert "`shell`" in out
    assert "`command`" in out
    # Concrete sample so the model has a shape to copy.
    assert "yf quote 005930.KS" in out
    # Forbids the wrong tool names that small models reach for.
    assert "NOT `bash`" in out or "not `bash`" in out
    assert "list_dir" in out  # forbidden in the prelude
    assert "echo" in out.lower()  # also forbidden post-Hynix-regression
    # Forbids skill_run for this skill.
    assert "MUST NOT be invoked via `skill_run`" in out
    # Korean ticker hint preserved.
    assert "005930" in out and ".KS" in out


def test_prelude_for_knowledge_skill_without_shell_demands_honest_refusal() -> None:
    """Hynix regression: when `shell` is NOT in the agent's tool registry
    (the default assistant is read-only), the model previously called
    `echo` with the intended command, treated the echoed string as a
    real result, and fabricated a 155,000원 / +2.1% price. The prelude
    must instead tell the model honestly that the skill cannot be
    executed in this agent — and explicitly forbid fabrication."""
    sk = _yahoo_finance_skill()
    s = suggest_skill_for("하이닉스 주가", [sk])
    assert s is not None
    # Tool registry of a typical default assistant (no shell).
    out = render_skill_suggestion_prelude(
        s,
        "하이닉스 주가",
        available_tool_names={"echo", "read_file", "list_dir", "grep", "glob"},
    )
    # Honest-refusal framing.
    assert "NOT EXECUTABLE" in out or "cannot be executed" in out
    # Must NOT instruct the model to call shell — it doesn't exist here.
    assert "`shell` tool call" not in out
    assert "command:" not in out.lower()
    # Must explicitly forbid the wrong tools (echo + list_dir + skill_run).
    assert "echo" in out.lower()
    assert "list_dir" in out
    assert "skill_run" in out
    # Anti-hallucination is the whole point of this branch.
    assert "fabricate" in out.lower()
    assert "prices" in out.lower() or "price" in out.lower()


def test_prelude_anti_hallucination_footer_present_in_both_branches() -> None:
    """The anti-hallucination footer (no inventing numbers, recognise
    `echo` mirrors as non-results) must land in every prelude variant,
    not just the no-shell branch."""
    sk_knowledge = _yahoo_finance_skill()
    s_k = suggest_skill_for("주가", [sk_knowledge])
    assert s_k is not None
    out_with_shell = render_skill_suggestion_prelude(
        s_k, "주가", available_tool_names={"shell"}
    )
    out_without_shell = render_skill_suggestion_prelude(
        s_k, "주가", available_tool_names={"echo"}
    )
    s_script = suggest_skill_for("주가", [_stock_skill()])
    assert s_script is not None
    out_script = render_skill_suggestion_prelude(s_script, "주가")
    for label, out in (
        ("knowledge+shell", out_with_shell),
        ("knowledge-no-shell", out_without_shell),
        ("script", out_script),
    ):
        assert "verbatim echo" in out.lower(), f"{label} missing echo guard"
        assert "fabricate" in out.lower() or "invent" in out.lower(), (
            f"{label} missing fabrication guard"
        )


def test_prelude_for_script_skill_still_uses_skill_run() -> None:
    """Regression guard: the existing skill_run prelude still fires for
    script-style skills (bodies that reference `scripts/<file>.py`)."""
    s = suggest_skill_for("삼성전자 주가", [_stock_skill()])
    assert s is not None and s.slug == "stock-analysis"
    out = render_skill_suggestion_prelude(s, "삼성전자 주가")
    assert "skill_run" in out
    # The forbid wording from the knowledge branch must NOT appear.
    assert "knowledge-style" not in out.lower()
    assert "analyze_stock.py" in out
