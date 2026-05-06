"""Tests for the opt-in binary installer.

The base `SkillInstaller` deliberately never auto-runs brew/apt/npm. The
`bin_installer` module + `oxenclaw skills install-bins` CLI cover the
explicit-consent path: each install spec becomes a `PlannedStep`; the
user confirms (or `--yes`); only confirmed steps run; argv is built
from a per-kind whitelist regex; `exec`/`download` are refused; and on
Linux when brew is absent we fall back to apt.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from oxenclaw.clawhub.bin_installer import (
    PlannedStep,
    execute,
    find_installed_skill,
    plan_install,
)
from oxenclaw.clawhub.frontmatter import SkillManifest, parse_skill_text
from oxenclaw.cli.__main__ import app

runner = CliRunner()


def _manifest(install_yaml_block: str) -> SkillManifest:
    """Build a SkillManifest with the given install-spec YAML block."""
    body = f"""---
name: dummy
description: Test fixture.
metadata:
  openclaw:
    requires:
      bins: [foo]
    install:
{install_yaml_block}
---

# body
"""
    manifest, _body = parse_skill_text(body)
    return manifest


# ---------- plan_install ----------


def test_plan_brew_uses_brew_when_present():
    m = _manifest('      - {id: jq, kind: brew, formula: jq, label: "Install jq"}')
    [step] = plan_install(m, host_os="linux", brew_present=True)
    assert step.decision == "run"
    assert step.argv == ("brew", "install", "jq")
    assert step.effective_kind == "brew"


def test_plan_brew_falls_back_to_apt_on_linux_when_brew_missing():
    m = _manifest('      - {kind: brew, formula: jq}')
    [step] = plan_install(m, host_os="linux", brew_present=False)
    assert step.decision == "run"
    assert step.argv == ("apt-get", "install", "-y", "jq")
    assert step.effective_kind == "brew→apt-fallback"


def test_plan_brew_refuses_when_no_brew_and_not_linux():
    m = _manifest('      - {kind: brew, formula: jq}')
    [step] = plan_install(m, host_os="darwin", brew_present=False)
    assert step.decision == "skip"
    assert "brew not on PATH" in step.reason


def test_plan_node_builds_global_install_argv():
    m = _manifest('      - {kind: node, package: yahoo-finance2}')
    [step] = plan_install(m, host_os="linux", brew_present=True)
    assert step.argv == ("npm", "install", "-g", "yahoo-finance2")


def test_plan_node_accepts_scoped_package():
    m = _manifest('      - {kind: node, package: "@scope/cli"}')
    [step] = plan_install(m, host_os="linux", brew_present=True)
    assert step.argv == ("npm", "install", "-g", "@scope/cli")


def test_plan_apt_builds_apt_get_argv():
    m = _manifest('      - {kind: apt, package: ripgrep}')
    [step] = plan_install(m, host_os="linux", brew_present=False)
    assert step.argv == ("apt-get", "install", "-y", "ripgrep")


def test_plan_pip_uv_go_kinds():
    m = _manifest(
        "      - {kind: pip, package: requests}\n"
        "      - {kind: uv, package: ruff}\n"
        "      - {kind: go, module: github.com/x/y}\n"
    )
    s_pip, s_uv, s_go = plan_install(m, host_os="linux", brew_present=True)
    assert s_pip.argv == ("pip", "install", "requests")
    assert s_uv.argv == ("uv", "tool", "install", "ruff")
    assert s_go.argv == ("go", "install", "github.com/x/y@latest")


def test_plan_skips_exec_with_manual_hint():
    m = _manifest('      - {kind: exec, command: "ln -sf a b"}')
    [step] = plan_install(m, host_os="linux", brew_present=True)
    assert step.decision == "skip"
    assert "manually" in step.reason


def test_plan_skips_download_with_manual_hint():
    m = _manifest('      - {kind: download, url: "https://x/y.tar.gz"}')
    [step] = plan_install(m, host_os="linux", brew_present=True)
    assert step.decision == "skip"
    assert "manually" in step.reason


def test_plan_rejects_unsafe_package_name():
    """Anything with a shell metachar must not reach argv."""
    m = _manifest('      - {kind: node, package: "evil; rm -rf /"}')
    [step] = plan_install(m, host_os="linux", brew_present=True)
    assert step.decision == "skip"
    assert "unsafe" in step.reason


def test_plan_unknown_kind_is_skipped():
    m = _manifest('      - {kind: rubygems, package: foo}')
    [step] = plan_install(m, host_os="linux", brew_present=True)
    assert step.decision == "skip"


# ---------- execute ----------


class _StubPrompter:
    def __init__(self, decisions: list[bool]) -> None:
        self._decisions = list(decisions)
        self.notifications: list[str] = []

    def confirm(self, step: PlannedStep) -> bool:
        return self._decisions.pop(0)

    def notify(self, message: str) -> None:
        self.notifications.append(message)


def _ok_proc(_: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def _fail_proc(_: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="oh no\nE: not found\n"
    )


def test_execute_runs_argv_when_confirmed():
    m = _manifest('      - {kind: node, package: foo}')
    plan = plan_install(m, host_os="linux", brew_present=True)
    calls: list[tuple[str, ...]] = []

    def runner_(argv):  # type: ignore[no-untyped-def]
        calls.append(tuple(argv))
        return _ok_proc(argv)

    [r] = execute(plan, _StubPrompter([True]), runner=runner_)
    assert calls == [("npm", "install", "-g", "foo")]
    assert r.executed and r.exit_code == 0


def test_execute_skips_when_declined():
    m = _manifest('      - {kind: node, package: foo}')
    plan = plan_install(m, host_os="linux", brew_present=True)

    def runner_(argv):  # type: ignore[no-untyped-def]
        raise AssertionError("must not run when user declines")

    [r] = execute(plan, _StubPrompter([False]), runner=runner_)
    assert not r.executed and r.stderr_tail == "declined"


def test_execute_dry_run_never_invokes_runner():
    m = _manifest('      - {kind: node, package: foo}')
    plan = plan_install(m, host_os="linux", brew_present=True)

    def runner_(argv):  # type: ignore[no-untyped-def]
        raise AssertionError("dry-run must not invoke runner")

    [r] = execute(plan, _StubPrompter([True]), dry_run=True, runner=runner_)
    assert not r.executed and r.stderr_tail == "dry-run"


def test_execute_propagates_failure_with_stderr_tail():
    m = _manifest('      - {kind: node, package: foo}')
    plan = plan_install(m, host_os="linux", brew_present=True)
    [r] = execute(plan, _StubPrompter([True]), runner=_fail_proc)
    assert r.executed and r.exit_code == 1
    assert r.stderr_tail and "not found" in r.stderr_tail


def test_execute_skipped_step_does_not_prompt():
    m = _manifest('      - {kind: exec, command: "echo hi"}')
    plan = plan_install(m, host_os="linux", brew_present=True)
    prompter = _StubPrompter([])  # would IndexError if confirm() were called
    [r] = execute(plan, prompter, runner=_ok_proc)
    assert not r.executed
    assert any("SKIP" in n for n in prompter.notifications)


# ---------- find_installed_skill + CLI ----------


def _write_yahoo_skill(home: Path) -> None:
    skill_dir = home / "skills" / "yahoo-finance-cli"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: yahoo-finance
description: stock prices.
metadata:
  openclaw:
    requires:
      bins: [jq, yf]
    install:
      - {id: jq, kind: brew, formula: jq, label: "Install jq"}
      - {id: yf, kind: node, package: yahoo-finance2, label: "Install yf"}
      - {id: link, kind: exec, command: "ln -sf a b", label: "Link yf"}
---

# body
"""
    )


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    return tmp_path


def test_find_installed_skill_returns_none_when_missing(isolated_home):
    assert find_installed_skill("nope") is None


def test_find_installed_skill_finds_user_skill(isolated_home):
    _write_yahoo_skill(isolated_home)
    s = find_installed_skill("yahoo-finance-cli")
    assert s is not None and s.slug == "yahoo-finance-cli"


def test_cli_install_bins_dry_run_does_not_run(isolated_home, monkeypatch):
    _write_yahoo_skill(isolated_home)
    monkeypatch.setattr(
        "oxenclaw.clawhub.bin_installer.platform.system", lambda: "Linux"
    )
    monkeypatch.setattr(
        "oxenclaw.clawhub.bin_installer._on_path", lambda name: name == "brew"
    )

    def _no_run(argv):  # type: ignore[no-untyped-def]
        raise AssertionError("dry-run must not invoke subprocess")

    monkeypatch.setattr("oxenclaw.clawhub.bin_installer._default_runner", _no_run)

    result = runner.invoke(
        app, ["skills", "install-bins", "yahoo-finance-cli", "--yes", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "brew install jq" in result.output
    assert "npm install -g yahoo-finance2" in result.output
    assert "SKIP" in result.output  # exec step
    assert "summary: 0 ok, 0 failed, 3 skipped" in result.output


def test_cli_install_bins_yes_runs_each_step(isolated_home, monkeypatch):
    _write_yahoo_skill(isolated_home)
    monkeypatch.setattr(
        "oxenclaw.clawhub.bin_installer.platform.system", lambda: "Linux"
    )
    # Force the apt-fallback branch to assert it triggers cleanly.
    monkeypatch.setattr(
        "oxenclaw.clawhub.bin_installer._on_path", lambda name: False
    )
    calls: list[tuple[str, ...]] = []

    def _runner(argv):  # type: ignore[no-untyped-def]
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(args=list(argv), returncode=0, stdout="", stderr="")

    monkeypatch.setattr("oxenclaw.clawhub.bin_installer._default_runner", _runner)
    result = runner.invoke(
        app, ["skills", "install-bins", "yahoo-finance-cli", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        ("apt-get", "install", "-y", "jq"),
        ("npm", "install", "-g", "yahoo-finance2"),
    ]
    assert "summary: 2 ok, 0 failed, 1 skipped" in result.output


def test_cli_install_bins_unknown_slug_errors(isolated_home):
    result = runner.invoke(app, ["skills", "install-bins", "ghost", "--yes"])
    assert result.exit_code == 1
    assert "not installed" in result.output


def test_cli_install_bins_propagates_failure_exit_code(isolated_home, monkeypatch):
    _write_yahoo_skill(isolated_home)
    monkeypatch.setattr(
        "oxenclaw.clawhub.bin_installer.platform.system", lambda: "Linux"
    )
    monkeypatch.setattr(
        "oxenclaw.clawhub.bin_installer._on_path", lambda name: name == "brew"
    )

    def _runner(argv):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(
            args=list(argv), returncode=1, stdout="", stderr="boom\n"
        )

    monkeypatch.setattr("oxenclaw.clawhub.bin_installer._default_runner", _runner)
    result = runner.invoke(
        app, ["skills", "install-bins", "yahoo-finance-cli", "--yes"]
    )
    assert result.exit_code == 1
    assert "✗" in result.output or "failed" in result.output


