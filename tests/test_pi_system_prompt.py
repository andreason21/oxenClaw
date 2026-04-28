"""Unit tests for `oxenclaw.pi.system_prompt`.

Covers the openclaw-ported behavioural sections (Execution Bias,
Skills mandatory, Memory Recall) and the project-context loader. The
PiAgent integration test in `test_pi_agent.py` exercises the full
assembly path; here we lock down the contracts of each contribution
in isolation so a wording / priority regression is caught fast.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oxenclaw.pi.system_prompt import (
    PROJECT_CONTEXT_FILES,
    SystemPromptContribution,
    assemble_system_prompt,
    embedded_context_contribution,
    execution_bias_contribution,
    load_project_context_files,
    memory_contribution,
    memory_recall_contribution,
    skills_contribution,
    skills_mandatory_contribution,
)


def test_execution_bias_contribution_is_cacheable_and_static() -> None:
    c = execution_bias_contribution()
    assert c.cacheable is True
    assert c.priority == 15
    # Body must contain the trigger keywords openclaw guarantees so the
    # model sees actionable / weak-result / mutable-fact phrasing.
    assert "## Execution Bias" in c.body
    assert "Actionable" in c.body
    assert "Weak/empty tool result" in c.body
    assert "Mutable facts" in c.body


def test_skills_mandatory_contribution_orders_above_skills_xml() -> None:
    procedure = skills_mandatory_contribution()
    xml = skills_contribution(skills_block="<available_skills></available_skills>")
    assert procedure.priority < xml.priority, (
        "procedure must sort ABOVE the XML block so the model reads the rule before the data"
    )
    assert "exactly one skill clearly applies" in procedure.body
    assert "never read more than one skill up front" in procedure.body


def test_memory_recall_contribution_orders_above_recalled_memories() -> None:
    procedure = memory_recall_contribution()
    xml = memory_contribution(memory_block="<recalled_memories></recalled_memories>")
    assert procedure.priority < xml.priority
    # The citation token convention must survive verbatim — UIs grep
    # for `[mem:` to highlight memory-backed assertions.
    assert "[mem:<id>]" in procedure.body
    assert "memory_search" in procedure.body
    # Procedural text is static → cacheable; XML block is per-turn data.
    assert procedure.cacheable is True
    assert xml.cacheable is False


def test_assemble_orders_new_contributions_consistently() -> None:
    """Full ordering smoke test: time / execution_bias / skills_mandatory /
    skills_xml / embedded_context / memory_recall / memory_xml all
    appear in the priority-sorted order the runtime depends on."""
    contributions = [
        memory_contribution(memory_block="<recalled_memories>X</recalled_memories>"),
        memory_recall_contribution(),
        embedded_context_contribution(files_block="# Project Context\n\nfoo"),
        skills_contribution(skills_block="<available_skills>Y</available_skills>"),
        skills_mandatory_contribution(),
        execution_bias_contribution(),
        SystemPromptContribution(
            name="time", body="Current time: 2026-04-27 (UTC)", priority=10, cacheable=False
        ),
    ]
    prompt, prefix_len = assemble_system_prompt("BASE", contributions)
    indices = {
        "base": prompt.find("BASE"),
        "time": prompt.find("Current time"),
        "exec_bias": prompt.find("## Execution Bias"),
        "skills_proc": prompt.find("## Skills (mandatory)"),
        "skills_xml": prompt.find("<available_skills>"),
        "embedded": prompt.find("# Project Context"),
        "memory_proc": prompt.find("## Memory Recall"),
        "memory_xml": prompt.find("<recalled_memories>"),
    }
    for k, v in indices.items():
        assert v >= 0, f"{k} missing from assembled prompt"
    # Strictly increasing position → priority ordering preserved.
    ordered = [
        indices["base"],
        indices["time"],
        indices["exec_bias"],
        indices["skills_proc"],
        indices["skills_xml"],
        indices["embedded"],
        indices["memory_proc"],
        indices["memory_xml"],
    ]
    assert ordered == sorted(ordered), ordered
    # Cacheable prefix must include everything except the dynamic memory
    # XML and the non-cacheable time block (in their respective slots).
    # Loosest invariant: the prefix length is at least the count of the
    # cacheable-only contributions (4: exec_bias, skills_proc, skills_xml,
    # embedded, memory_proc → 5, but `time` is non-cacheable so the last
    # cacheable index can sit after it). We only check a sane lower bound.
    assert prefix_len >= 5


def test_load_project_context_files_returns_empty_when_dir_missing(tmp_path: Path) -> None:
    assert load_project_context_files(None) == ""
    assert load_project_context_files(tmp_path / "does-not-exist") == ""
    # Directory exists but holds no canonical filenames → empty.
    (tmp_path / "unrelated.txt").write_text("hello")
    assert load_project_context_files(tmp_path) == ""


def test_load_project_context_files_emits_canonical_order(tmp_path: Path) -> None:
    # Write files in REVERSE of canonical order to verify the loader
    # sorts by `PROJECT_CONTEXT_FILES`, not by filesystem order.
    (tmp_path / "memory.md").write_text("MEM body")
    (tmp_path / "AGENTS.md").write_text("AGENTS body")
    (tmp_path / "SOUL.md").write_text("SOUL body")
    block = load_project_context_files(tmp_path)
    assert block, "expected non-empty block when canonical files exist"
    assert "# Project Context" in block
    # Canonical order: AGENTS → SOUL → memory (skipping the absent ones).
    idx_agents = block.find("AGENTS body")
    idx_soul = block.find("SOUL body")
    idx_memory = block.find("MEM body")
    assert idx_agents >= 0 and idx_soul >= 0 and idx_memory >= 0
    assert idx_agents < idx_soul < idx_memory


def test_load_project_context_files_truncates_runaway_file(tmp_path: Path) -> None:
    big = "X" * 50_000
    (tmp_path / "AGENTS.md").write_text(big)
    block = load_project_context_files(tmp_path, max_chars_per_file=1_000)
    assert "[truncated]" in block
    # The truncation cap is per-file; the rendered section must still
    # be well under the runaway size.
    assert len(block) < 5_000


def test_project_context_files_constant_matches_openclaw_set() -> None:
    """Lock down the canonical filename set so a refactor doesn't
    silently drop a file that operators rely on (e.g. SOUL.md persona)."""
    assert PROJECT_CONTEXT_FILES == (
        "AGENTS.md",
        "SOUL.md",
        "identity.md",
        "user.md",
        "tools.md",
        "bootstrap.md",
        "memory.md",
    )


def test_load_project_context_files_is_case_insensitive(tmp_path: Path) -> None:
    # Operator wrote `agents.md` in lowercase; loader still picks it up
    # but renders the canonical heading.
    (tmp_path / "agents.md").write_text("body")
    block = load_project_context_files(tmp_path)
    assert "## AGENTS.md" in block, block


@pytest.mark.parametrize(
    "func,name",
    [
        (execution_bias_contribution, "execution_bias"),
        (skills_mandatory_contribution, "skills_mandatory"),
        (memory_recall_contribution, "memory_recall"),
    ],
)
def test_static_contributions_have_stable_names(func, name) -> None:
    """The `name` attribute is what observability + dedup keys off of —
    accidental renames break dashboards."""
    assert func().name == name
