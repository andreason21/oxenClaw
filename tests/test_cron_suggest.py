"""Tests for the cron / schedule suggestion prelude."""

from __future__ import annotations

from oxenclaw.agents.cron_suggest import (
    detect_cron_request,
    render_cron_suggestion_prelude,
)

# ─── detect_cron_request ──────────────────────────────────────────────


def test_returns_none_when_no_interval_keyword() -> None:
    # Time mention alone is not enough — the user is just asking *about*
    # 8 o'clock, not asking to schedule something.
    assert detect_cron_request("아침 8시에 뭐해야 돼?") is None
    assert detect_cron_request("at 9am the market opens") is None


def test_returns_none_for_unrelated_text() -> None:
    assert detect_cron_request("") is None
    assert detect_cron_request("hello") is None
    assert detect_cron_request("나스닥 주가 알려줘") is None


def test_korean_daily_morning_time_parses_to_cron() -> None:
    s = detect_cron_request(
        "매일 아침 8시 50분에 나스닥, S&P, 환율, 오일 수치 리포트 만들어서 알려줘"
    )
    assert s is not None
    assert s.schedule == "50 8 * * *"
    assert "매일" in s.matched_keywords
    assert s.raw_time_text and "8" in s.raw_time_text


def test_korean_evening_time_promotes_to_pm() -> None:
    s = detect_cron_request("매일 저녁 7시에 알려줘")
    assert s is not None
    assert s.schedule == "0 19 * * *"


def test_english_daily_at_9am() -> None:
    s = detect_cron_request("every day at 9am send me a summary")
    assert s is not None
    assert s.schedule == "0 9 * * *"


def test_weekly_with_korean_weekday() -> None:
    s = detect_cron_request("매주 월요일 오전 10시 30분에 회의록 정리해줘")
    assert s is not None
    # weekday=1 (Mon), hour=10, minute=30
    assert s.schedule == "30 10 * * 1"


def test_hourly_without_explicit_time() -> None:
    s = detect_cron_request("매시간 핑 보내줘")
    assert s is not None
    assert s.schedule == "0 * * * *"


def test_interval_only_falls_through_to_none_schedule() -> None:
    # Interval fired but no parseable time → suggestion still returned,
    # schedule left None so the model fills it.
    s = detect_cron_request("매일 알려줘")
    assert s is not None
    assert s.schedule is None
    assert s.matched_keywords == ["매일"]


# ─── render_cron_suggestion_prelude ───────────────────────────────────


def test_prelude_names_cron_tool_and_action_add() -> None:
    s = detect_cron_request("매일 아침 8시 50분에 리포트 알려줘")
    assert s is not None
    out = render_cron_suggestion_prelude(s, "매일 아침 8시 50분에 리포트 알려줘")
    assert "cron" in out
    assert '"add"' in out
    assert "50 8 * * *" in out


def test_prelude_forbids_markdown_howto() -> None:
    """The prelude has to actively block the failure mode we observed:
    the model writes a `crontab` how-to instead of issuing a tool call."""
    s = detect_cron_request("every day at 9am send me a summary")
    assert s is not None
    out = render_cron_suggestion_prelude(s, "every day at 9am send me a summary")
    assert "Do NOT" in out
    assert "crontab" in out  # mentions what NOT to do
    assert "tool call" in out


def test_prelude_does_not_paste_python_call_template() -> None:
    """Mirrors the skill_suggest invariant — small models echo
    name(arg=val) templates verbatim into the assistant body."""
    s = detect_cron_request("매일 8시에 알려줘")
    assert s is not None
    out = render_cron_suggestion_prelude(s, "매일 8시에 알려줘")
    assert "cron(" not in out
    assert 'action="' not in out


def test_prelude_tells_model_to_strip_temporal_phrase_from_prompt() -> None:
    """Critical for breaking the cron-fire feedback loop: the `prompt`
    field must NOT carry the schedule phrase. Otherwise every fire
    re-registers a duplicate job (the registered prompt itself trips
    `detect_cron_request` on the next turn)."""
    s = detect_cron_request("매일 아침 8시 50분에 리포트 알려줘")
    assert s is not None
    out = render_cron_suggestion_prelude(s, "매일 아침 8시 50분에 리포트 알려줘")
    assert "prompt" in out
    assert "loops the scheduler" in out
    # The example must show the cleaned prompt, not the verbatim text.
    assert "시장 리포트 알려줘" in out


def test_prelude_describes_blank_schedule_when_unparseable() -> None:
    s = detect_cron_request("매일 알려줘")
    assert s is not None
    out = render_cron_suggestion_prelude(s, "매일 알려줘")
    # No concrete cron string was parsed; prelude must still tell the
    # model what shape `schedule` should take.
    assert "5-field crontab" in out


# ─── interaction with the original failure case ───────────────────────


def test_user_morning_report_request_yields_actionable_prelude() -> None:
    """The actual user input that exposed the bug. The prelude must
    contain everything the model needs to convert it into a cron tool
    call without further reasoning."""
    user_text = (
        "매일 아침 8시50분에 나스닥, S&P, 환율, 오일 수치 리포트 만들어서 알려줘"
    )
    s = detect_cron_request(user_text)
    assert s is not None
    assert s.schedule == "50 8 * * *"
    out = render_cron_suggestion_prelude(s, user_text)
    assert "50 8 * * *" in out
    assert "cron" in out
    assert "add" in out
