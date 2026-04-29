"""Regex backstop for personal-fact extraction.

Locks the small set of high-precision shapes the regex layer is
*supposed* to catch — anything outside this set is the per-turn
LLM dreamer's job (`oxenclaw.memory.turn_dream`).
"""

from __future__ import annotations

from oxenclaw.memory.auto_extract import extract_personal_facts


def test_ko_family_x_is_my_relation() -> None:
    """Original shape: '<name>는 우리 <relation>이야'."""
    facts = extract_personal_facts("민수는 우리 형이야")
    assert facts == [
        "사용자의 형은 민수이다. The user's 형 (older brother) is 민수.",
    ]


def test_ko_family_internal_relation_name() -> None:
    """Bug fix: '<owner> <relation> 이름은 <name>' shape was missed
    pre-fix, so '나의 형 이름은 민수야' never reached memory."""
    expected = "사용자의 형은 민수이다. The user's 형 (older brother) is 민수."
    for text in (
        "나의 형 이름은 민수야",
        "우리 형 이름은 민수야",
        "내 형 이름은 민수야",
    ):
        assert extract_personal_facts(text) == [expected], text


def test_ko_family_jonsaengmal() -> None:
    """존댓말 variant: '제 누나 이름은 수지입니다' — 입니다 stripped."""
    facts = extract_personal_facts("제 누나 이름은 수지입니다")
    assert facts == [
        "사용자의 누나는 수지이다. The user's 누나 (older sister) is 수지.",
    ]


def test_ko_josa_eun_neun_picks_correct_particle() -> None:
    """받침 차이로 은/는이 달라야 함."""
    # 형 ends without 받침 → 은
    assert "형은" in extract_personal_facts("민수는 우리 형이야")[0]
    # 누나 ends without 받침 → 는
    assert "누나는" in extract_personal_facts("수지는 우리 누나야")[0]


def test_ko_self_name() -> None:
    facts = extract_personal_facts("내 이름은 영희야")
    assert facts == ["사용자의 이름은 영희이다. The user's name is 영희."]


def test_ko_location() -> None:
    facts = extract_personal_facts("나는 수원에 살아")
    assert facts == ["사용자는 수원에 거주한다. The user lives in 수원."]


def test_en_family() -> None:
    facts = extract_personal_facts("Bob is my brother")
    assert facts == ["The user's brother is Bob."]


def test_en_self_name() -> None:
    facts = extract_personal_facts("My name is Andrew")
    assert facts == ["The user's name is Andrew."]


def test_en_location() -> None:
    facts = extract_personal_facts("I live in Seoul")
    assert facts == ["The user lives in Seoul."]


def test_question_form_does_not_match() -> None:
    """Questions are not facts — '나의 형 이름 뭐야?' must NOT extract.
    The recall layer handles that side; matching here would store
    garbage in memory."""
    assert extract_personal_facts("나의 형 이름 뭐야?") == []
    assert extract_personal_facts("내가 어디 살지?") == []


def test_empty_input() -> None:
    assert extract_personal_facts("") == []
    assert extract_personal_facts("   ") == []


def test_dedup_within_message() -> None:
    """Same fact stated twice → emitted once."""
    facts = extract_personal_facts("민수는 우리 형이야. 민수는 우리 형이야.")
    assert facts == [
        "사용자의 형은 민수이다. The user's 형 (older brother) is 민수.",
    ]


def test_relation_word_is_not_a_name() -> None:
    """Captured 'name' that's actually a relation token is dropped to
    avoid '사용자의 형은 누나이다' kinds of nonsense."""
    # "누나는 우리 형이야" — '누나' captured as the name slot but it's
    # in _KO_RELATIONS, so the renderer must skip.
    assert extract_personal_facts("누나는 우리 형이야") == []
