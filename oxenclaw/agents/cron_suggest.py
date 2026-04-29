"""Per-turn cron/schedule suggestion prelude.

Small local models routinely interpret "매일 아침 8시 50분에 리포트 보내줘"
as a request for *advice* — they reply with a markdown how-to about
`crontab` instead of issuing the registered `cron` tool call. Symptom in
the trace: `tool_calls=0`, `finish=stop`, `content` describing the
feature in prose. Mirrors the pattern of `skill_suggest`: detect at turn
ingestion, prepend a tight user-side directive that tells the model
exactly which tool to use and what to put in each argument.

Keep the heuristic high-precision: a false positive looks like the model
being lectured about cron when the user just asked a question. Two
gates must fire together — a scheduling **interval** keyword (매일 /
daily / every day / 매주 / weekly / ...) AND a recognisable **time**
expression (8시 50분 / 8:50am / at 9). Either one alone is too noisy.

The module also returns a best-effort 5-field crontab string when both
gates fire so the model has a concrete value to copy into the tool's
`schedule` field. The original user text is intended to flow through
into the cron job's `prompt` field unchanged — that's what the
scheduler will re-fire as a synthetic user message at the appointed
time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ─── interval keywords ────────────────────────────────────────────────

_INTERVAL_KEYWORDS: frozenset[str] = frozenset(
    {
        # daily
        "매일", "매아침", "daily", "everyday",
        # weekly
        "매주", "weekly",
        # monthly
        "매월", "매달", "monthly",
        # hourly / minutely
        "매시간", "매시", "hourly", "매분", "minutely",
        # explicit cron / schedule words
        "cron", "crontab", "schedule", "scheduled", "예약", "정기",
        "스케줄", "스케쥴", "매번", "주기적",
    }
)
_INTERVAL_PHRASES = (
    "every day", "every morning", "every afternoon", "every evening",
    "every week", "every hour", "every minute",
)

# ─── time-of-day patterns ─────────────────────────────────────────────

# Korean: "8시 50분", "오전 9시", "저녁 7시 30분"
_KO_TIME_RE = re.compile(
    r"(?:(?P<period>오전|오후|아침|저녁|밤|낮)\s*)?"
    r"(?P<hour>\d{1,2})\s*시"
    r"(?:\s*(?P<minute>\d{1,2})\s*분)?"
)
# English: "8:50", "9am", "7:30 pm", "at 9"
_EN_TIME_RE = re.compile(
    r"(?:at\s+)?"
    r"(?P<hour>\d{1,2})"
    r"(?::(?P<minute>\d{2}))?"
    r"\s*(?P<ampm>am|pm|AM|PM)?",
)

_KO_WEEKDAYS: dict[str, int] = {
    "월요일": 1, "화요일": 2, "수요일": 3, "목요일": 4,
    "금요일": 5, "토요일": 6, "일요일": 0,
    "월요": 1, "화요": 2, "수요": 3, "목요": 4,
    "금요": 5, "토요": 6, "일요": 0,
}
_EN_WEEKDAYS: dict[str, int] = {
    "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4,
    "friday": 5, "saturday": 6, "sunday": 0,
    "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 0,
}


@dataclass
class CronSuggestion:
    """Detected scheduling intent + the best-effort cron expression."""

    schedule: str | None  # 5-field crontab, or None if we couldn't parse a time
    matched_keywords: list[str]
    raw_time_text: str | None  # the snippet we parsed the hour/minute from


def _has_interval_signal(text_lower: str) -> list[str]:
    """Return the matched interval keywords/phrases (empty when none)."""
    matched: list[str] = []
    for kw in _INTERVAL_KEYWORDS:
        if kw in text_lower:
            matched.append(kw)
    for phrase in _INTERVAL_PHRASES:
        if phrase in text_lower:
            matched.append(phrase)
    return matched


def _parse_time(text: str) -> tuple[int | None, int | None, str | None]:
    """Return (hour, minute, raw_match_text) — minute defaults to 0 when
    only the hour was present. Korean parser runs first because the EN
    regex is greedy and matches digit-only strings the user didn't
    actually intend as times."""
    m = _KO_TIME_RE.search(text)
    if m:
        try:
            h = int(m.group("hour"))
            mm = int(m.group("minute") or 0)
        except (TypeError, ValueError):
            return None, None, None
        period = m.group("period") or ""
        if period in {"오후", "저녁", "밤"} and h < 12:
            h += 12
        if period in {"오전", "아침"} and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= mm <= 59:
            return h, mm, m.group(0)
    m = _EN_TIME_RE.search(text)
    if m:
        try:
            h = int(m.group("hour"))
            mm = int(m.group("minute") or 0)
        except (TypeError, ValueError):
            return None, None, None
        ampm = (m.group("ampm") or "").lower()
        if ampm == "pm" and h < 12:
            h += 12
        if ampm == "am" and h == 12:
            h = 0
        if 0 <= h <= 23 and 0 <= mm <= 59:
            return h, mm, m.group(0)
    return None, None, None


def _pick_weekday(text_lower: str) -> int | None:
    for word, dow in _KO_WEEKDAYS.items():
        if word in text_lower:
            return dow
    for word, dow in _EN_WEEKDAYS.items():
        # word-boundary match for English to avoid "monitor" → "mon"
        if re.search(rf"\b{word}\b", text_lower):
            return dow
    return None


def _build_cron(
    hour: int, minute: int, *, weekly: bool, weekday: int | None
) -> str:
    """Return a 5-field crontab expression for the parsed time."""
    if weekly and weekday is not None:
        return f"{minute} {hour} * * {weekday}"
    return f"{minute} {hour} * * *"


def detect_cron_request(user_text: str) -> CronSuggestion | None:
    """Return a `CronSuggestion` when the user message clearly asks to
    schedule something on a recurring cadence. Returns None otherwise.

    Two-gate detector:
      - at least one *interval* keyword/phrase, AND
      - at least one parseable *time-of-day* expression.
    Both signals together are enough — the false-positive shape (e.g.
    a question that just *mentions* "8시" without asking for a schedule)
    won't have an interval keyword and is rejected.
    """
    if not user_text:
        return None
    text = user_text.strip()
    text_lower = text.lower()
    intervals = _has_interval_signal(text_lower)
    if not intervals:
        return None
    hour, minute, raw_time = _parse_time(text)
    if hour is None:
        # Interval fired but no time → still a schedule intent (e.g.
        # "매시간 핑 보내줘"). Default to the top of the hour for
        # `매시간`, otherwise leave schedule None and let the model
        # fill it.
        if any(kw in text_lower for kw in ("매시간", "매시", "hourly")):
            return CronSuggestion(
                schedule="0 * * * *",
                matched_keywords=intervals,
                raw_time_text=None,
            )
        return CronSuggestion(
            schedule=None, matched_keywords=intervals, raw_time_text=None
        )
    weekly = any(
        kw in text_lower
        for kw in ("매주", "weekly", "every week")
    )
    weekday = _pick_weekday(text_lower) if weekly else None
    schedule = _build_cron(hour, minute, weekly=weekly, weekday=weekday)
    return CronSuggestion(
        schedule=schedule,
        matched_keywords=intervals,
        raw_time_text=raw_time,
    )


def render_cron_suggestion_prelude(
    suggestion: CronSuggestion,
    user_text: str,
) -> str:
    """Tight prelude prepended to the user message.

    Names the `cron` tool, gives a concrete `schedule` string when one
    was parseable, and tells the model that the original user request
    text becomes the cron job's `prompt` field. Deliberately avoids
    showing a Python-style call template (small models echo those into
    the assistant body — see the matching note in `skill_suggest`)."""
    schedule_line = (
        f"  - schedule: {suggestion.schedule!r}"
        if suggestion.schedule
        else "  - schedule: a 5-field crontab string for the cadence the user named"
    )
    matched = ", ".join(suggestion.matched_keywords[:5])
    time_hint = (
        f" (parsed time: {suggestion.raw_time_text!r})"
        if suggestion.raw_time_text
        else ""
    )
    return (
        "[SCHEDULE REQUEST DETECTED] The user is asking to register a "
        f"recurring task (matched: {matched}){time_hint}. Do NOT explain "
        "how to set up cron, write a bash script, or paste a crontab "
        "snippet — the runtime has a real `cron` tool. Issue a real tool "
        "call to `cron` via the tool-call channel with these arguments:\n"
        '  - action: "add"\n'
        f"{schedule_line}\n"
        "  - prompt: the user's original request, verbatim, so the "
        "scheduler can re-fire it as a synthetic user message at the "
        "scheduled time\n"
        "  - description: a short label summarising what the job does "
        "(e.g. \"daily market report\")\n"
        "Leave agent_id / channel / account_id / chat_id unset — the "
        "tool fills those from the calling context.\n"
        "If you fall back to writing the call as JSON in your reply "
        "text instead of a structured tool_use block, include the tool "
        "name as a `tool` field alongside the arguments so the autofire "
        "backstop can route it correctly."
    )


__all__ = [
    "CronSuggestion",
    "detect_cron_request",
    "render_cron_suggestion_prelude",
]
