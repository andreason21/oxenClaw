"""Phase T4: skill runtime — env_overrides + ephemeral workspace."""

from __future__ import annotations

from pathlib import Path

import pytest

from oxenclaw.clawhub.frontmatter import parse_skill_text
from oxenclaw.clawhub.loader import InstalledSkill
from oxenclaw.clawhub.runtime import (
    prepare_skill_runtime,
    resolve_env_overrides,
)
from oxenclaw.config.paths import OxenclawPaths


def _skill(metadata: str | None = None) -> InstalledSkill:
    """Build a minimal InstalledSkill from inline frontmatter."""
    md = "---\nname: t-skill\ndescription: t\n"
    if metadata:
        md += metadata
    md += "---\n\nbody"
    manifest, body = parse_skill_text(md)
    return InstalledSkill(
        slug="t-skill",
        manifest=manifest,
        skill_md_path=Path("/tmp/t-skill/SKILL.md"),
        body=body,
        origin=None,
    )


def _paths(tmp_path: Path) -> OxenclawPaths:
    p = OxenclawPaths(home=tmp_path)
    p.ensure_home()
    return p


# ─── env expansion ──────────────────────────────────────────────────


def test_resolve_expands_dollar_var() -> None:
    out = resolve_env_overrides(
        {"K": "$THE_TOKEN", "L": "literal"},
        host_env={"THE_TOKEN": "abc-123"},
    )
    assert out == {"K": "abc-123", "L": "literal"}


def test_resolve_expands_braced_form() -> None:
    out = resolve_env_overrides({"K": "prefix-${X}-suffix"}, host_env={"X": "MID"})
    assert out == {"K": "prefix-MID-suffix"}


def test_resolve_unset_var_becomes_empty() -> None:
    out = resolve_env_overrides({"K": "$NONEXISTENT"}, host_env={})
    assert out == {"K": ""}


def test_resolve_handles_no_dollar_path() -> None:
    out = resolve_env_overrides({"K": "no-vars-here"}, host_env={})
    assert out == {"K": "no-vars-here"}


def test_resolve_coerces_non_strings() -> None:
    out = resolve_env_overrides({"K": 42, "B": True}, host_env={})
    assert out == {"K": "42", "B": "True"}


def test_resolve_empty_returns_empty() -> None:
    assert resolve_env_overrides(None) == {}
    assert resolve_env_overrides({}) == {}


# ─── workspace lifecycle ────────────────────────────────────────────


def test_workspace_ephemeral_created_and_removed(tmp_path: Path) -> None:
    skill = _skill()
    paths = _paths(tmp_path)
    captured: list[Path] = []
    with prepare_skill_runtime(skill, paths=paths) as rt:
        assert rt.workspace_dir.exists()
        captured.append(rt.workspace_dir)
        # Write a file into the workspace.
        (rt.workspace_dir / "scratch.txt").write_text("hi")
    assert not captured[0].exists()


def test_workspace_persistent_kind_persists(tmp_path: Path) -> None:
    skill = _skill("openclaw:\n  workspace:\n    kind: persistent\n")
    paths = _paths(tmp_path)
    with prepare_skill_runtime(skill, paths=paths) as rt:
        assert rt.config.kind == "persistent"
        ws = rt.workspace_dir
        (ws / "f.txt").write_text("x")
    # Persistent dir survives.
    assert ws.exists()
    assert (ws / "f.txt").exists()


def test_workspace_retains_on_error_when_configured(tmp_path: Path) -> None:
    skill = _skill("openclaw:\n  workspace:\n    kind: ephemeral\n    retain_on_error: true\n")
    paths = _paths(tmp_path)
    captured: list[Path] = []
    with pytest.raises(RuntimeError), prepare_skill_runtime(skill, paths=paths) as rt:
        captured.append(rt.workspace_dir)
        raise RuntimeError("boom")
    # Retained because the config asked for it.
    assert captured[0].exists()


def test_workspace_removed_on_error_when_not_retained(tmp_path: Path) -> None:
    skill = _skill()  # default: retain_on_error=False
    paths = _paths(tmp_path)
    captured: list[Path] = []
    with pytest.raises(RuntimeError), prepare_skill_runtime(skill, paths=paths) as rt:
        captured.append(rt.workspace_dir)
        raise RuntimeError("nope")
    assert not captured[0].exists()


# ─── env on the runtime ─────────────────────────────────────────────


def test_runtime_env_includes_skill_overrides(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("VAULT_TOKEN", "secret-xyz")
    skill = _skill(
        'openclaw:\n  env_overrides:\n    MY_TOKEN: "$VAULT_TOKEN"\n    LOG_LEVEL: "debug"\n'
    )
    paths = _paths(tmp_path)
    with prepare_skill_runtime(skill, paths=paths) as rt:
        assert rt.env["MY_TOKEN"] == "secret-xyz"
        assert rt.env["LOG_LEVEL"] == "debug"
        # Baseline env (PATH/HOME) is also present.
        assert "PATH" in rt.env
        assert rt.env["HOME"] == str(tmp_path)


def test_runtime_extra_env_overrides_skill_env(tmp_path: Path) -> None:
    skill = _skill('openclaw:\n  env_overrides:\n    X: "from-skill"\n')
    paths = _paths(tmp_path)
    with prepare_skill_runtime(skill, paths=paths, extra_env={"X": "from-caller"}) as rt:
        assert rt.env["X"] == "from-caller"


def test_runtime_workspace_root_under_home(tmp_path: Path) -> None:
    skill = _skill()
    paths = _paths(tmp_path)
    with prepare_skill_runtime(skill, paths=paths) as rt:
        assert rt.workspace_dir.parent == tmp_path / "skill-workspaces"
        assert rt.workspace_dir.name.startswith("oxenclaw-t-skill-")
