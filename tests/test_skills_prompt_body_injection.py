"""`format_skills_for_prompt` now injects a SKILL.md body excerpt
into each `<skill>` block so the model can pick the right script +
args without making a separate read_file call. Locks the contract
that:
  - body excerpt appears under `<usage>` for skills with non-empty
    body
  - long bodies are truncated cleanly (no half-cut fenced code blocks)
  - the directive in the leading `<usage>` block tells the model to
    use `skill_run`, not to invent a tool named after the skill
"""

from __future__ import annotations

from pathlib import Path

from oxenclaw.clawhub.frontmatter import SkillManifest
from oxenclaw.clawhub.loader import InstalledSkill, format_skills_for_prompt


def _skill(slug: str, body: str) -> InstalledSkill:
    manifest = SkillManifest.model_validate({"name": slug, "description": f"{slug} desc"})
    return InstalledSkill(
        slug=slug,
        manifest=manifest,
        skill_md_path=Path(f"/fake/{slug}/SKILL.md"),
        body=body,
        origin=None,
    )


def test_empty_skill_list_returns_empty() -> None:
    assert format_skills_for_prompt([]) == ""


def test_top_level_directive_mentions_skill_run() -> None:
    out = format_skills_for_prompt([_skill("demo", "body")])
    # Must steer the model toward the actual tool, not toward
    # inventing one.
    assert "skill_run" in out
    assert "skill_resolver" in out


def test_body_excerpt_is_injected() -> None:
    body = "## Quick Commands\n\nuv run scripts/foo.py AAPL\n"
    out = format_skills_for_prompt([_skill("demo", body)])
    assert "Quick Commands" in out
    assert "scripts/foo.py" in out
    # Slug + name + description still present.
    assert "<slug>demo</slug>" in out
    assert "<name>demo</name>" in out


def test_long_body_is_truncated_with_marker() -> None:
    body = "## Section\n\n" + ("filler line\n" * 1000)
    out = format_skills_for_prompt([_skill("big", body)], body_chars=400)
    assert "truncated" in out


def test_truncation_does_not_cut_inside_fenced_code_block() -> None:
    """If the body cap falls inside ```…``` the excerpt walks back
    so no half-fence escapes into the prompt."""
    body = (
        "Intro paragraph (long enough to push the cap past the open fence).\n"
        "More intro text padding lines.\n" + ("padding line\n" * 30) + "```python\n"
        "uv run scripts/foo.py\n"
        "still inside fence\n"
        "```\n"
    )
    # Cap chosen to land mid-fence.
    out = format_skills_for_prompt([_skill("fenced", body)], body_chars=520)
    # Find the skill's <usage> excerpt block (the one inside <skill>,
    # not the top-level directive that mentions skill_run).
    body_block = out.split("<usage>")[2]  # [0]=before, [1]=top-level, [2]=skill-level
    body_block = body_block.split("</usage>")[0]
    fence_count = body_block.count("```")
    assert fence_count % 2 == 0, (
        f"excerpt left an unterminated ``` fence; got {fence_count} fences in: {body_block!r}"
    )


def test_multiple_skills_each_carry_excerpt() -> None:
    skills = [
        _skill("alpha", "## Alpha quick start\nuv run scripts/a.py\n"),
        _skill("beta", "## Beta basics\nbash scripts/b.sh\n"),
    ]
    out = format_skills_for_prompt(skills)
    assert "Alpha quick start" in out
    assert "Beta basics" in out
    assert out.count("<skill>") == 2


def test_empty_body_skill_omits_usage_block() -> None:
    """Skills with no body (just frontmatter) should still appear
    in the catalog — just without a `<usage>` excerpt."""
    out = format_skills_for_prompt([_skill("nobody", "  \n")])
    # The skill itself appears.
    assert "<slug>nobody</slug>" in out
    # No per-skill <usage> tag (only the top-level one).
    inside_skill = out.split("<skill>")[1].split("</skill>")[0]
    assert "<usage>" not in inside_skill
