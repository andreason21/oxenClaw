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
from pathlib import Path
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


def _is_knowledge_style(skill: Any, body: str) -> bool:
    """True when a skill ships no `scripts/` directory and is meant to be
    invoked via the `bash` shell tool with the CLI commands documented
    in its SKILL.md body (e.g. `yahoo-finance-cli` runs `yf quote …`).

    Detection prefers the on-disk check (`<skill_dir>/scripts/` has at
    least one regular file). When `skill.skill_md_path` isn't available
    — typically in unit tests using lightweight fakes — we fall back to
    a body heuristic: a body with no `scripts/<file>` reference but at
    least one fenced bash/shell block is treated as knowledge-style.
    """
    skill_md = getattr(skill, "skill_md_path", None)
    if isinstance(skill_md, Path):
        scripts_dir = skill_md.parent / "scripts"
        if scripts_dir.is_dir():
            for entry in scripts_dir.iterdir():
                if entry.is_file():
                    return False
        return True
    if _INVOCATION_RE.search(body or ""):
        return False
    return bool(re.search(r"```(?:bash|sh|shell)\b", body or ""))


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


_KO_TICKER_HINT = (
    "For Korean stock tickers use the Yahoo Finance form "
    "<6-digit>.KS (KOSPI) / .KQ (KOSDAQ) — 삼성전자 = 005930.KS, "
    "SK하이닉스 = 000660.KS.\n"
)

# Appended to every prelude variant. The yahoo-finance-cli regression
# loop showed two real failure modes that small local models fall into
# once the "do not refuse" directive is in place:
#
#   1. Pick the first tool whose argument shape vaguely resembles a
#      command (the assistant's default registry has `echo` but no
#      `shell`), call it with the intended command string, then treat
#      the verbatim echo as if it were a real result.
#   2. Fabricate concrete numeric values (prices, percentages, market
#      caps) to satisfy the user — worse than the original refusal
#      because the answer looks authoritative.
#
# Both fail loudly to a human reviewer but silently to a casual user.
# The footer is the cheapest available counter — explicit prohibition.
_ANTI_HALLUCINATION_FOOTER = (
    "If the only tool result you obtain is the verbatim echo of your "
    "input (e.g. an `echo` tool returning the same string you sent), "
    "that means NO execution happened — say so honestly to the user. "
    "Under no circumstances may you invent numeric values such as "
    "prices, percentages, market caps, dates, or any other concrete "
    "data point that you did not get from a real tool result."
)


def _render_script_prelude(suggestion: SkillSuggestion, body: str) -> str:
    """Existing path: skill ships `scripts/`; route the model at `skill_run`."""
    slug = suggestion.slug
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
        + _KO_TICKER_HINT
        + "If you fall back to writing the call as JSON in your reply "
        "text instead of a structured tool_use block, include the tool "
        "name as a `tool` field alongside the arguments so the autofire "
        "backstop can route it correctly.\n"
        + _ANTI_HALLUCINATION_FOOTER
    )


def _render_knowledge_prelude(
    suggestion: SkillSuggestion,
    *,
    shell_available: bool,
) -> str:
    """Knowledge-style skills have no `scripts/` — they document a CLI
    in their SKILL.md body and expect the model to invoke that CLI via
    the registered `shell` tool directly.

    Three failure modes the wording here is calibrated against:
      1. Routing through `skill_run` returns "(scripts/ dir empty or
         missing)" and the model paraphrases that back as a refusal.
      2. Saying "use the bash tool" makes small models look for a tool
         literally named `bash`, fail to find one, and fall into a
         `list_dir` exploration loop.
      3. When `shell` is NOT in the agent's tool registry (the default
         assistant is read-only — `shell` is gated behind an
         ApprovalManager), small models pick the closest-named tool
         (`echo` with a `text` arg) and treat the echoed string as a
         real result, then fabricate plausible numbers. That is worse
         than honest refusal because it looks authoritative. We branch
         on `shell_available` so the model is never told to call a
         tool that doesn't exist for this agent.
    """
    slug = suggestion.slug
    if shell_available:
        return (
            f"[INSTALLED SKILL DETECTED] The user's request matches the installed "
            f"`{slug}` skill (matched: {', '.join(suggestion.matched_terms)}). "
            f"This is a knowledge-style skill — it ships NO `scripts/` directory "
            "and MUST NOT be invoked via `skill_run` (that returns '(scripts/ "
            "dir empty or missing)'). The registered tool to use is named "
            "exactly `shell` (NOT `bash`, NOT `list_dir`, NOT `echo`); it takes "
            "a single `command` argument that goes through `sh -c`. Read the "
            f"documented CLI commands in the `{slug}` <usage> block above and "
            "invoke them via a `shell` tool call — for example, for a stock "
            "price query the call is `shell` with "
            "`command: \"yf quote 005930.KS\"`. Do NOT respond with a generic "
            "'I can't access real-time data' refusal, do NOT enumerate the "
            "skill directory with `list_dir`, do NOT route the command "
            "through `echo` (that just mirrors your text and runs nothing), "
            "and do NOT write the command as plain text in your reply — the "
            "runtime only sees structured tool calls.\n"
            + _KO_TICKER_HINT
            + _ANTI_HALLUCINATION_FOOTER
        )
    return (
        f"[INSTALLED SKILL DETECTED, BUT NOT EXECUTABLE IN THIS AGENT] The "
        f"user's request matches the installed `{slug}` skill (matched: "
        f"{', '.join(suggestion.matched_terms)}), which is a knowledge-style "
        "skill that needs a `shell` tool to actually run its documented CLI "
        "commands. THIS AGENT'S TOOL REGISTRY HAS NO `shell` TOOL, so the "
        "skill cannot be executed in this conversation. Tell the user "
        "honestly that you can describe the skill but cannot fetch real "
        f"data — name `{slug}` so they know which capability is gated, and "
        "suggest enabling the shell tool (gateway-side configuration) if "
        "they need real execution. Do NOT call `skill_run` (no scripts), "
        "do NOT call `echo` and treat its mirrored text as a result, do "
        "NOT call `list_dir` to enumerate the skill, and absolutely DO NOT "
        "fabricate prices, percentages, market caps, or any other concrete "
        "values in lieu of a real result.\n"
        + _ANTI_HALLUCINATION_FOOTER
    )


def render_skill_suggestion_prelude(
    suggestion: SkillSuggestion,
    user_text: str,
    *,
    available_tool_names: set[str] | None = None,
) -> str:
    """Tight prelude prepended to the user message.

    Branches on skill style and on tool availability. Script-style
    skills (`<skill>/scripts/` populated) get the `skill_run` shape.
    Knowledge-style skills (e.g. `yahoo-finance-cli` — only SKILL.md,
    documents a CLI) get a directive pointing at the registered
    `shell` tool when one is in `available_tool_names`. When `shell`
    is NOT available (the default assistant agent's read-only bundle),
    we instead emit an honest-refusal prelude — telling the model the
    skill is detected but unexecutable in this agent and explicitly
    forbidding fabrication. That branch exists because the previous
    "use the shell tool" wording, when issued to an agent without a
    shell tool, drove the model to call `echo` and then hallucinate
    a stock price from the echoed command string.

    Deliberately *avoids* showing a literal `name(arg=val)` expression
    in any branch — small local models will copy that expression
    into the assistant text instead of issuing a real tool call,
    defeating the whole point of the prelude.

    `available_tool_names` defaults to None for callers that haven't
    been updated yet; in that case we assume `shell` IS available so
    existing behavior is preserved (the script branch is unchanged
    and the knowledge branch falls back to its original wording).

    User-side (not system-side) for the same reason as
    `format_memories_as_prelude`: small local models attend much
    more strongly to user-message context."""
    skill = suggestion.skill
    body = getattr(skill, "body", "") or ""
    if _is_knowledge_style(skill, body):
        shell_available = (
            True if available_tool_names is None else "shell" in available_tool_names
        )
        return _render_knowledge_prelude(suggestion, shell_available=shell_available)
    return _render_script_prelude(suggestion, body)


__all__ = [
    "SkillSuggestion",
    "render_skill_suggestion_prelude",
    "suggest_skill_for",
]
