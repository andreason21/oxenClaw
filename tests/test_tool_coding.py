"""Phase T5: coding_agent tool tests."""

from __future__ import annotations

from pathlib import Path

from oxenclaw.clawhub.frontmatter import parse_skill_text
from oxenclaw.clawhub.loader import InstalledSkill
from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.tools_pkg.coding import (
    coding_agent_tool,
    detect_available_clis,
)


def _paths(tmp_path: Path) -> OxenclawPaths:
    p = OxenclawPaths(home=tmp_path)
    p.ensure_home()
    return p


def _fake_cli(tmp_path: Path, name: str, *, exit_code: int = 0, stdout: str = "ok\n") -> Path:
    """Create an executable `name` script in `tmp_path` printing `stdout`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / name
    script.write_text(f"#!/usr/bin/env bash\nprintf '%s' \"{stdout}\"\nexit {exit_code}\n")
    script.chmod(0o755)
    return bin_dir


# ─── detection ──────────────────────────────────────────────────────


def test_detect_available_clis_empty_when_path_isolated(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    out = detect_available_clis()
    assert out == []


def test_detect_finds_claude_when_on_path(tmp_path: Path, monkeypatch) -> None:
    bd = _fake_cli(tmp_path, "claude")
    monkeypatch.setenv("PATH", f"{bd}:/usr/bin:/bin")
    out = detect_available_clis()
    assert "claude" in out


# ─── tool execution ─────────────────────────────────────────────────


async def test_tool_runs_first_available_cli(tmp_path: Path, monkeypatch) -> None:
    bd = _fake_cli(tmp_path, "codex", stdout="codex did the thing")
    monkeypatch.setenv("PATH", f"{bd}:/usr/bin:/bin")
    tool = coding_agent_tool(paths=_paths(tmp_path))
    out = await tool.execute({"task": "hello world"})
    assert "coding_agent[codex] ok" in out
    assert "codex did the thing" in out


async def test_tool_respects_explicit_cli_choice(tmp_path: Path, monkeypatch) -> None:
    bd = _fake_cli(tmp_path, "claude", stdout="claude here")
    _fake_cli(tmp_path, "codex", stdout="codex here")
    monkeypatch.setenv("PATH", f"{bd}:/usr/bin:/bin")
    tool = coding_agent_tool(paths=_paths(tmp_path))
    out = await tool.execute({"task": "x", "cli": "claude"})
    assert "claude here" in out


async def test_tool_reports_when_no_cli_available(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    tool = coding_agent_tool(paths=_paths(tmp_path))
    out = await tool.execute({"task": "x"})
    assert "no CLI available" in out


async def test_tool_truncates_huge_stdout(tmp_path: Path, monkeypatch) -> None:
    big = "Z" * 50_000
    bd = _fake_cli(tmp_path, "codex", stdout=big)
    monkeypatch.setenv("PATH", f"{bd}:/usr/bin:/bin")
    tool = coding_agent_tool(paths=_paths(tmp_path))
    out = await tool.execute({"task": "x", "max_stdout_chars": 1000})
    assert "[...truncated" in out


async def test_tool_surfaces_nonzero_exit(tmp_path: Path, monkeypatch) -> None:
    bd = _fake_cli(tmp_path, "codex", exit_code=2, stdout="boom-out")
    monkeypatch.setenv("PATH", f"{bd}:/usr/bin:/bin")
    tool = coding_agent_tool(paths=_paths(tmp_path))
    out = await tool.execute({"task": "x"})
    assert "exit=2" in out


async def test_tool_runs_in_ephemeral_workspace(tmp_path: Path, monkeypatch) -> None:
    """The CLI's CWD should be inside skill-workspaces/."""
    bd = tmp_path / "bin"
    bd.mkdir(parents=True, exist_ok=True)
    script = bd / "codex"
    # Print pwd so we can assert it's under skill-workspaces.
    script.write_text("#!/usr/bin/env bash\npwd\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bd}:/usr/bin:/bin")
    paths = _paths(tmp_path)
    tool = coding_agent_tool(paths=paths)
    out = await tool.execute({"task": "x"})
    assert "skill-workspaces" in out


async def test_tool_uses_supplied_skill_for_env(tmp_path: Path, monkeypatch) -> None:
    """When a skill with env_overrides is supplied, the CLI sees that env."""
    bd = tmp_path / "bin"
    bd.mkdir(parents=True, exist_ok=True)
    script = bd / "codex"
    script.write_text('#!/usr/bin/env bash\necho "X=$X"\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bd}:/usr/bin:/bin")

    md = (
        "---\nname: coding-agent\ndescription: t\n"
        'openclaw:\n  env_overrides:\n    X: "injected-value"\n---\n'
    )
    manifest, body = parse_skill_text(md)
    skill = InstalledSkill(
        slug="coding-agent",
        manifest=manifest,
        skill_md_path=Path("/tmp/SKILL.md"),
        body=body,
        origin=None,
    )
    tool = coding_agent_tool(skill=skill, paths=_paths(tmp_path))
    out = await tool.execute({"task": "x"})
    assert "X=injected-value" in out
