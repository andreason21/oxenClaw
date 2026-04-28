"""Regex-based personal-fact extractor for incoming user messages.

The system prompt asks the model to call `memory_save(text=...)` when
the user states a durable personal fact (name, family, location,
preference). Small local models (gemma/qwen/llama-3.2) frequently
skip that side-effect call and focus only on producing a textual
reply. Result: in a 2-turn exchange like

    user (turn 1): "미누는 우리 형이야."
    user (turn 2): "우리 형 이름 뭐야?"

…nothing was ever written to memory in turn 1, so the recall in
turn 2 finds zero hits and the agent looks like it forgot.

This module is a deterministic backstop: scan the user's text for a
small, high-precision set of personal-fact shapes (Korean + English)
and emit complete natural-language sentences that
`MemoryRetriever.save()` can append to the inbox. Bilingual rendering
(KO + EN sentence in one chunk) is intentional — the embedding store
hits cross-language queries that way.

It does NOT replace `memory_save`; the model can and should still
call it for facts the regex doesn't catch (preferences, decisions,
free-form knowledge). This just covers the common shapes that small
models reliably fail on.
"""

from __future__ import annotations

import re
from collections.abc import Callable

# Each pattern compiles a regex + a rendering function. The render
# function takes the match and returns a natural-sentence fact (or an
# empty string to skip — useful when the captured group is too short
# to be meaningful).

_NAME_TOK = r"[가-힣A-Za-z][가-힣A-Za-z0-9_\-]{0,30}"

_KO_RELATIONS = (
    "형",
    "누나",
    "언니",
    "오빠",
    "동생",
    "엄마",
    "아빠",
    "아버지",
    "어머니",
    "할머니",
    "할아버지",
    "아들",
    "딸",
    "남편",
    "아내",
    "와이프",
    "남친",
    "여친",
    "남자친구",
    "여자친구",
    "애인",
    "친구",
    "선생님",
    "팀장",
    "상사",
)

_EN_RELATIONS = (
    "brother",
    "sister",
    "mother",
    "father",
    "mom",
    "dad",
    "son",
    "daughter",
    "husband",
    "wife",
    "boyfriend",
    "girlfriend",
    "partner",
    "friend",
    "manager",
    "boss",
    "teacher",
    "grandmother",
    "grandfather",
)


def _ko_family(m: re.Match[str]) -> str:
    name = _strip_ko_ending(m.group(1).strip())
    rel = m.group(2)
    if len(name) < 1 or name in _KO_RELATIONS:  # avoid "우리는 우리 형..." nonsense
        return ""
    josa = _josa_eun_neun(rel)  # 형 → 은,  누나 → 는
    return (
        f"사용자의 {rel}{josa} {name}이다. The user's {rel} ({_ko_to_en_relation(rel)}) is {name}."
    )


def _josa_eun_neun(word: str) -> str:
    """Pick the right Korean topic particle for `word` ('은' vs '는')."""
    if not word:
        return "는"
    last = word[-1]
    code = ord(last)
    if 0xAC00 <= code <= 0xD7A3:  # Hangul syllable
        # (code - 0xAC00) % 28 == 0 means no final consonant (받침)
        return "는" if (code - 0xAC00) % 28 == 0 else "은"
    return "는"


# Common KO sentence-final tokens that creep into a captured name when
# the regex is greedy; strip them so "영희야" → "영희".
_KO_ENDINGS = (
    "입니다",
    "이에요",
    "예요",
    "이야",
    "이다",
    "에요",
    "야",
    "임",
)


def _strip_ko_ending(name: str) -> str:
    for end in _KO_ENDINGS:
        if name.endswith(end) and len(name) > len(end):
            return name[: -len(end)]
    return name


def _ko_to_en_relation(rel: str) -> str:
    table = {
        "형": "older brother",
        "누나": "older sister",
        "언니": "older sister",
        "오빠": "older brother",
        "동생": "younger sibling",
        "엄마": "mother",
        "아빠": "father",
        "아버지": "father",
        "어머니": "mother",
        "할머니": "grandmother",
        "할아버지": "grandfather",
        "아들": "son",
        "딸": "daughter",
        "남편": "husband",
        "아내": "wife",
        "와이프": "wife",
        "남친": "boyfriend",
        "여친": "girlfriend",
        "남자친구": "boyfriend",
        "여자친구": "girlfriend",
        "애인": "partner",
        "친구": "friend",
        "선생님": "teacher",
        "팀장": "team lead",
        "상사": "manager",
    }
    return table.get(rel, rel)


def _ko_name(m: re.Match[str]) -> str:
    name = _strip_ko_ending(m.group(1).strip())
    if len(name) < 1:
        return ""
    return f"사용자의 이름은 {name}이다. The user's name is {name}."


def _ko_location(m: re.Match[str]) -> str:
    place = _strip_ko_ending(m.group(1).strip())
    if len(place) < 1:
        return ""
    return f"사용자는 {place}에 거주한다. The user lives in {place}."


def _en_family(m: re.Match[str]) -> str:
    name = m.group(1).strip()
    rel = m.group(2).lower()
    if len(name) < 2:
        return ""
    return f"The user's {rel} is {name}."


def _en_name(m: re.Match[str]) -> str:
    name = m.group(1).strip()
    if len(name) < 2:
        return ""
    return f"The user's name is {name}."


def _en_location(m: re.Match[str]) -> str:
    place = m.group(1).strip().rstrip(".,!?")
    if len(place) < 2:
        return ""
    return f"The user lives in {place}."


# The order matters only for documentation — every pattern runs on
# every input and dedupes downstream.
_PATTERNS: list[tuple[re.Pattern[str], Callable[[re.Match[str]], str]]] = [
    # Korean: "<name>는/은 우리 <relation>이야/이다/임/(이)에요" — the
    # exact shape from "미누는 우리 형이야".
    (
        re.compile(
            rf"({_NAME_TOK})\s*(?:는|은)\s*우리\s*({'|'.join(_KO_RELATIONS)})",
        ),
        _ko_family,
    ),
    # Korean: "<name>(이)는 내 <relation>이야" — the "내" variant.
    (
        re.compile(
            rf"({_NAME_TOK})(?:이)?\s*(?:는|은)\s*내\s*({'|'.join(_KO_RELATIONS)})",
        ),
        _ko_family,
    ),
    # Korean self-name: "내/제 이름은 <name>" / "나는 <name>(이)야".
    (
        re.compile(rf"(?:내|제)\s*이름은\s*({_NAME_TOK})"),
        _ko_name,
    ),
    # Korean location: "나는 <place>에 살아 / 살고 / 거주" or
    # "<place>에서 살아".
    (
        re.compile(
            rf"(?:나는|저는|내가|제가)\s*({_NAME_TOK})\s*(?:에서?|에)\s*"
            rf"(?:살아요?|살고\s*있|살아\s*있|거주)"
        ),
        _ko_location,
    ),
    # English family: "<Name> is my <relation>".
    (
        re.compile(
            rf"\b([A-Z][\w\-]{{1,30}})\s+is\s+my\s+({'|'.join(_EN_RELATIONS)})\b",
            re.IGNORECASE,
        ),
        _en_family,
    ),
    # English self-name: "My name is X" / "I'm X" / "I am X".
    (
        re.compile(
            r"(?:my\s+name\s+is|i\s*am|i'?m)\s+([A-Z][\w\-]{1,30})\b",
            re.IGNORECASE,
        ),
        _en_name,
    ),
    # English location: "I live in X".
    (
        re.compile(r"i\s+live\s+in\s+([A-Z][\w\- ]{1,40})", re.IGNORECASE),
        _en_location,
    ),
]


def extract_personal_facts(text: str) -> list[str]:
    """Return a deduped list of natural-sentence facts for ``text``.

    Empty list when no shape matched. Designed to be cheap (regex
    only) so callers can fire it on every user message without adding
    perceptible latency.

    Each emitted fact is a complete sentence — bilingual KO+EN where
    the source was Korean — suitable for direct
    ``MemoryRetriever.save(text, tags=["auto"])`` ingestion.
    """
    if not text or not text.strip():
        return []
    facts: list[str] = []
    seen: set[str] = set()
    for pattern, render in _PATTERNS:
        for m in pattern.finditer(text):
            try:
                fact = render(m)
            except Exception:
                continue
            if not fact or fact in seen:
                continue
            seen.add(fact)
            facts.append(fact)
    return facts


__all__ = ["extract_personal_facts"]
