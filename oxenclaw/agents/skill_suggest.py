"""Per-turn installed-skill suggestion prelude.

The `<available_skills>` system-prompt block surfaces every installed
skill, but small local models (gemma2/3, qwen2.5:3b, llama3.2:8b)
routinely ignore it — when asked "삼성전자 주가" they fall back to
the canned "I can't access real-time data" refusal even though
`stock-analysis` is installed and `skill_run` is a registered tool.

This module supplies a keyword-driven prelude:

  - `suggest_skill_for(text, skills)` — light token overlap between
    `text` and every installed skill's description; returns the
    best-matching `InstalledSkill` (and the matched keywords) when
    confidence is high enough.
  - `render_skill_suggestion_prelude(skill, matched, text)` — tight
    user-side directive that names the skill, points at `skill_run`,
    and surfaces a sample arg shape pulled from the SKILL.md body.

Mirrors the pattern of `oxenclaw.agents.pending_action`: detect at
turn ingestion, prepend to the user message body so attention is
high, fail-open when nothing matches.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# Light tokenizer — keep CJK runs intact, split ASCII on word boundary.
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[가-힣]+")

# Cross-lingual / synonym expansion for the most common skill
# domains. Maps any token in `aliases` to the canonical EN domain
# token, so a KO query ("주가") matches an EN skill description
# ("stock"). Skill manifests are usually written in English, but
# end-users ask in their native language — without this layer the
# token-overlap heuristic finds nothing and falls through to the
# refusal.
#
# Add new domains as you encounter false-negative installs in the
# wild. Keep this list focused; over-aliasing → false-positive
# suggestions which are worse than no suggestion.
_DOMAIN_SYNONYMS: dict[str, frozenset[str]] = {
    "stock": frozenset(
        {
            "stock",
            "stocks",
            "equity",
            "equities",
            "share",
            "shares",
            "price",
            "prices",
            "ticker",
            "trading",
            "finance",
            "financial",
            "주식",
            "주가",
            "시세",
            "종목",
            "코스피",
            "코스닥",
            "kospi",
            "kosdaq",
        }
    ),
    "crypto": frozenset(
        {
            "crypto",
            "cryptocurrency",
            "bitcoin",
            "btc",
            "eth",
            "ethereum",
            "암호화폐",
            "코인",
        }
    ),
    "portfolio": frozenset(
        {
            "portfolio",
            "watchlist",
            "holdings",
            "포트폴리오",
            "관심종목",
            "보유",
        }
    ),
    "weather": frozenset(
        {
            "weather",
            "temperature",
            "forecast",
            "rain",
            "snow",
            "날씨",
            "기온",
            "비",
            "눈",
            "예보",
        }
    ),
    "time": frozenset({"time", "clock", "now", "today", "시간", "시각", "지금"}),
    "github": frozenset({"github", "issue", "issues", "pr", "repo", "이슈", "저장소"}),
}


def _expand_synonyms(tokens: set[str]) -> set[str]:
    """Add the canonical domain key for any token that appears in a
    `_DOMAIN_SYNONYMS` alias set. Original tokens are preserved so
    direct matches still count."""
    expanded = set(tokens)
    for canon, aliases in _DOMAIN_SYNONYMS.items():
        if tokens & aliases:
            expanded.add(canon)
            # Also add EVERY alias of that canon — lets a KO user
            # token ("주가") match an EN description ("stock") via
            # the shared canonical "stock".
            expanded |= aliases
    return expanded


# Stop words that are TOO common to be useful as skill match signal.
# Includes generic verbs/nouns from both EN and KO.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "for",
        "on",
        "with",
        "is",
        "are",
        "was",
        "were",
        "be",
        "this",
        "that",
        "what",
        "how",
        "why",
        "when",
        "where",
        "who",
        "i",
        "me",
        "my",
        "you",
        "your",
        "use",
        "using",
        "show",
        "tell",
        "find",
        "get",
        "give",
        "please",
        "now",
        "today",
        "current",
        "알려",
        "알려줘",
        "보여",
        "보여줘",
        "해줘",
        "해주세요",
        "줘",
        "어떻게",
        "뭐",
        "뭐야",
        "어디",
        "언제",
        "왜",
        "누구",
        "지금",
        "오늘",
        "현재",
        "는",
        "은",
        "이",
        "가",
        "을",
        "를",
        "사용",
        "사용해",
        "사용해줘",
        "스킬",
        "툴",
    }
)


@dataclass
class SkillSuggestion:
    """A single ranked skill match for the current user message."""

    skill: Any  # InstalledSkill — kept untyped to avoid an import cycle
    matched_terms: list[str]
    score: float

    @property
    def slug(self) -> str:
        return getattr(self.skill, "slug", "") or getattr(self.skill, "name", "")


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _content_tokens(text: str) -> set[str]:
    return {t for t in _tokens(text) if t and t not in _STOPWORDS and len(t) >= 2}


def suggest_skill_for(
    user_text: str,
    skills: Iterable[Any],
    *,
    min_overlap: int = 1,
) -> SkillSuggestion | None:
    """Best-match installed skill for `user_text`, or None.

    Uses a cheap token-overlap heuristic between the user's message
    and each skill's `name` + `description` (both lowercased). The
    intent is high precision, not recall — false suggestions look
    like "tool refused" to the user, so we err on the side of None.

    `min_overlap` = how many distinct content tokens must match to
    suggest. Default 1 because skill descriptions are usually long
    and the user query is usually short, so even a single non-stop
    token (e.g. "주가" vs description containing "주가") is signal.
    """
    raw_user_tokens = _content_tokens(user_text)
    if not raw_user_tokens:
        return None
    # Expand both sides so a KO query ("주가") matches an EN
    # description ("stock") via the shared canonical domain key.
    user_tokens = _expand_synonyms(raw_user_tokens)

    best: SkillSuggestion | None = None
    for s in skills:
        bag = " ".join(
            [
                getattr(s, "name", "") or "",
                getattr(s, "slug", "") or "",
                getattr(s, "description", "") or "",
            ]
        )
        raw_skill_tokens = _content_tokens(bag)
        if not raw_skill_tokens:
            continue
        skill_tokens = _expand_synonyms(raw_skill_tokens)
        matched = sorted(user_tokens & skill_tokens)
        if len(matched) < min_overlap:
            continue
        # Surface the user's ORIGINAL tokens that contributed (not the
        # expanded synonyms) so the prelude says "matched: 주가" not
        # "matched: stock", which would confuse a KO-speaking user
        # debugging the trace.
        original_matched = sorted(raw_user_tokens & skill_tokens)
        report_terms = original_matched or matched
        # Score = matched count, biased by skill description length so
        # super-broad skills don't always win on coincidental hits.
        denom = max(len(skill_tokens), 1)
        score = len(matched) / (denom**0.5)
        if best is None or score > best.score:
            best = SkillSuggestion(skill=s, matched_terms=report_terms, score=score)
    return best


# Heuristic per-skill arg hints. Mostly: pull the first
# `uv run {baseDir}/scripts/<file>.py <SAMPLE>` line out of the body
# so the prelude can show the model a concrete shape.
_INVOCATION_RE = re.compile(
    r"(?:uv\s+run\s+)?(?:\{baseDir\}/)?scripts/(?P<script>[\w\-]+\.py)(?P<args>(?:\s+[^\n#`]+)?)",
)


def _extract_sample_invocation(body: str) -> tuple[str, str] | None:
    """Return (script, sample_args_string) from the first matching
    line in the SKILL.md body. None when nothing parseable found."""
    if not body:
        return None
    for line in body.splitlines():
        s = line.strip().lstrip("$").strip()
        if not s or s.startswith("#"):
            continue
        m = _INVOCATION_RE.search(s)
        if m is None:
            continue
        script = m.group("script")
        sample = (m.group("args") or "").strip()
        return script, sample
    return None


def render_skill_suggestion_prelude(
    suggestion: SkillSuggestion,
    user_text: str,
) -> str:
    """Tight prelude prepended to the user message.

    Names the matching skill and tells the model which `skill_run`
    arguments to fill in. Deliberately *avoids* showing a literal
    `name(arg=val)` expression — small local models will copy that
    expression into the assistant text instead of issuing a real
    tool call, defeating the whole point of the prelude. The shape
    is described in prose so the model has to reach for the
    structured tool-call channel.

    User-side (not system-side) for the same reason as
    `format_memories_as_prelude`: small local models attend much
    more strongly to user-message context."""
    skill = suggestion.skill
    slug = suggestion.slug
    body = getattr(skill, "body", "") or ""
    extracted = _extract_sample_invocation(body)
    if extracted is None:
        script_hint = "the script name shown in the skill's <usage> block"
        args_hint = "the values implied by the user's question"
    else:
        script, sample = extracted
        script_hint = f'"{script}"'
        args_hint = (
            f'a list whose first element resembles "{sample}", adjusted for the user\'s question'
            if sample
            else "the values implied by the user's question"
        )
    return (
        f"[INSTALLED SKILL DETECTED] The user's request matches the installed "
        f"`{slug}` skill (matched: {', '.join(suggestion.matched_terms)}). "
        "Do NOT respond with a generic 'I can't access real-time data' "
        "refusal, and do NOT write the call as plain text — the runtime only "
        "sees structured tool calls. Issue a real tool call to `skill_run` "
        "via the tool-call channel with these arguments:\n"
        f"  - skill: {slug}\n"
        f"  - script: {script_hint}\n"
        f"  - args: {args_hint}\n"
        "For Korean stock tickers use the Yahoo Finance form "
        "<6-digit>.KS (KOSPI) / .KQ (KOSDAQ) — 삼성전자 = 005930.KS, "
        "SK하이닉스 = 000660.KS.\n"
        "If you fall back to writing the call as JSON in your reply "
        "text instead of a structured tool_use block, include the tool "
        "name as a `tool` field alongside the arguments so the autofire "
        "backstop can route it correctly."
    )


__all__ = [
    "SkillSuggestion",
    "render_skill_suggestion_prelude",
    "suggest_skill_for",
]
