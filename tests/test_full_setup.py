"""Tests for `oxenclaw setup` / `setup all` — the one-shot full bootstrap.

Platform detection and the apt plan are pure functions, so they're
tested directly against injected `/etc/os-release` text. The wizard's IO
is stubbed end-to-end (no subprocess, no apt, no real token writes
outside `tmp_path`) so the whole flow runs in milliseconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner

from oxenclaw.flows.full_setup import FullSetupWizard
from oxenclaw.flows.system_deps import (
    apt_plan_for,
    detect_platform,
    non_apt_guidance,
)

_UBUNTU_2204 = 'ID=ubuntu\nVERSION_ID="22.04"\nVERSION_CODENAME=jammy\nID_LIKE=debian\n'
_UBUNTU_2404 = 'ID=ubuntu\nVERSION_ID="24.04"\nVERSION_CODENAME=noble\nID_LIKE=debian\n'
_DEBIAN_12 = 'ID=debian\nVERSION_ID="12"\nVERSION_CODENAME=bookworm\n'

# ─── Platform detection ──────────────────────────────────────────────


def test_detect_ubuntu_2404() -> None:
    p = detect_platform(
        system="Linux",
        os_release_text=_UBUNTU_2404,
        uname_release="6.6.0-generic",
        python_version=(3, 12),
    )
    assert p.is_ubuntu
    assert p.is_debian_like
    assert p.version_id == "24.04"
    assert p.codename == "noble"
    assert p.python_ok
    assert not p.is_wsl


def test_detect_ubuntu_2204_is_below_python_floor() -> None:
    p = detect_platform(
        system="Linux",
        os_release_text=_UBUNTU_2204,
        uname_release="5.15.0-generic",
        python_version=(3, 10),
    )
    assert p.version_id == "22.04"
    assert not p.python_ok


def test_detect_wsl_from_uname() -> None:
    p = detect_platform(
        system="Linux",
        os_release_text=_UBUNTU_2404,
        uname_release="5.15.167.4-microsoft-standard-WSL2",
        python_version=(3, 12),
    )
    assert p.is_wsl
    assert "WSL2" in p.pretty


def test_detect_macos_is_not_debian_like() -> None:
    p = detect_platform(system="Darwin", uname_release="23.0.0", python_version=(3, 12))
    assert not p.is_debian_like
    assert not p.is_ubuntu
    assert p.distro_id is None


def test_detect_debian_is_debian_like_via_id() -> None:
    p = detect_platform(
        system="Linux",
        os_release_text=_DEBIAN_12,
        uname_release="6.1.0",
        python_version=(3, 11),
    )
    assert p.is_debian_like
    assert not p.is_ubuntu


# ─── apt plan ────────────────────────────────────────────────────────


def test_apt_plan_2204_adds_deadsnakes_and_py312() -> None:
    p = detect_platform(system="Linux", os_release_text=_UBUNTU_2204, python_version=(3, 10))
    plan = apt_plan_for(p)
    assert "python3.12-venv" in plan.packages
    assert "python3-venv" not in plan.packages  # native pkg not used on 22.04
    # deadsnakes PPA must be added before the install.
    pre_heads = [c[0] for c in plan.pre_commands]
    assert "add-apt-repository" in pre_heads
    assert any("deadsnakes" in note.lower() for note in plan.notes)


def test_apt_plan_2404_uses_native_python_no_ppa() -> None:
    p = detect_platform(system="Linux", os_release_text=_UBUNTU_2404, python_version=(3, 12))
    plan = apt_plan_for(p)
    assert "python3-venv" in plan.packages
    assert "python3.12-venv" not in plan.packages
    assert plan.pre_commands == []


def test_apt_plan_always_includes_build_toolchain_and_sandbox() -> None:
    p = detect_platform(system="Linux", os_release_text=_UBUNTU_2404, python_version=(3, 12))
    plan = apt_plan_for(p)
    for pkg in ("build-essential", "git", "cmake", "bubblewrap"):
        assert pkg in plan.packages


def test_command_sequence_prefixes_sudo_and_refreshes_after_ppa() -> None:
    p = detect_platform(system="Linux", os_release_text=_UBUNTU_2204, python_version=(3, 10))
    seq = apt_plan_for(p).command_sequence(use_sudo=True)
    assert all(cmd[0] == "sudo" for cmd in seq)
    # The install command comes last and carries every package.
    assert seq[-1][1:4] == ["apt-get", "install", "-y"]
    # add-apt-repository is followed by a second `apt-get update`.
    joined = [" ".join(c) for c in seq]
    ppa_idx = next(i for i, s in enumerate(joined) if "add-apt-repository" in s)
    assert "apt-get update" in joined[ppa_idx + 1]


def test_command_sequence_without_sudo_when_root() -> None:
    p = detect_platform(system="Linux", os_release_text=_UBUNTU_2404, python_version=(3, 12))
    seq = apt_plan_for(p).command_sequence(use_sudo=False)
    assert all(cmd[0] != "sudo" for cmd in seq)


def test_non_apt_guidance_for_macos() -> None:
    p = detect_platform(system="Darwin", python_version=(3, 12))
    lines = non_apt_guidance(p)
    assert any("brew" in line for line in lines)


# ─── Wizard scaffolding ──────────────────────────────────────────────


@dataclass
class _StubPrompter:
    selects: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    confirms: list[bool] = field(default_factory=list)

    def select(self, message: str, choices: list[str], *, default: str | None = None) -> str:
        return self.selects.pop(0)

    def text(self, message: str, *, default: str | None = None, secret: bool = False) -> str:
        return self.texts.pop(0)

    def confirm(self, message: str, *, default: bool = True) -> bool:
        return self.confirms.pop(0)


@dataclass
class _StubIO:
    messages: list[str] = field(default_factory=list)
    commands: list[list[str]] = field(default_factory=list)
    command_rc: int = 0

    def emit(self, message: str) -> None:
        self.messages.append(message)

    def run_command(self, argv: list[str], *, timeout: int = 1800) -> int:
        self.commands.append(argv)
        return self.command_rc


def _ubuntu(version: str = "24.04", py: tuple[int, int] = (3, 12)):
    text = f'ID=ubuntu\nVERSION_ID="{version}"\nVERSION_CODENAME=test\nID_LIKE=debian\n'
    return detect_platform(system="Linux", os_release_text=text, python_version=py)


# ─── Wizard happy paths ──────────────────────────────────────────────


def test_wizard_runs_apt_when_confirmed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    prompter = _StubPrompter(confirms=[True])  # confirm apt
    io = _StubIO()
    wizard = FullSetupWizard(
        prompter=prompter,
        io=io,
        interactive=True,
        skip_provider=True,
        platform_override=_ubuntu("24.04"),
    )
    result = wizard.run()

    assert result.apt_attempted
    assert result.apt_ok is True
    # apt-get update + install were both executed.
    heads = [" ".join(c) for c in io.commands]
    assert any("apt-get update" in h for h in heads)
    assert any("apt-get install -y" in h for h in heads)
    # Config + token were scaffolded under the temp home.
    assert result.config_created
    assert (tmp_path / "config.yaml").is_file()
    assert (tmp_path / "gateway-token").is_file()
    assert result.token


def test_wizard_apt_failure_sets_not_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    prompter = _StubPrompter(confirms=[True])
    io = _StubIO(command_rc=100)  # first apt command fails
    wizard = FullSetupWizard(
        prompter=prompter,
        io=io,
        interactive=True,
        skip_provider=True,
        platform_override=_ubuntu("24.04"),
    )
    result = wizard.run()

    assert result.apt_attempted
    assert result.apt_ok is False
    # Stopped after the first failing command (didn't push the install).
    assert len(io.commands) == 1


def test_wizard_declined_apt_only_prints(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    prompter = _StubPrompter(confirms=[False])  # decline apt
    io = _StubIO()
    wizard = FullSetupWizard(
        prompter=prompter,
        io=io,
        interactive=True,
        skip_provider=True,
        platform_override=_ubuntu("24.04"),
    )
    result = wizard.run()

    assert result.apt_attempted is False
    assert io.commands == []  # nothing executed
    # But the config/token steps still ran.
    assert (tmp_path / "config.yaml").is_file()


def test_wizard_non_interactive_prints_apt_skips_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    io = _StubIO()
    wizard = FullSetupWizard(
        prompter=_StubPrompter(),  # no answers needed
        io=io,
        interactive=False,
        platform_override=_ubuntu("22.04", py=(3, 10)),
    )
    result = wizard.run()

    assert result.apt_attempted is False
    assert io.commands == []
    assert result.provider_choice is None
    # The printed sequence includes the deadsnakes PPA for 22.04.
    blob = "\n".join(io.messages)
    assert "add-apt-repository" in blob
    assert "deadsnakes" in blob


def test_wizard_skip_apt_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    io = _StubIO()
    wizard = FullSetupWizard(
        prompter=_StubPrompter(),
        io=io,
        interactive=True,
        skip_apt=True,
        skip_provider=True,
        platform_override=_ubuntu("24.04"),
    )
    result = wizard.run()
    assert result.apt_attempted is False
    assert io.commands == []


def test_wizard_preserves_existing_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    cfg = tmp_path / "config.yaml"
    cfg.write_text("agents: {}\n", encoding="utf-8")
    io = _StubIO()
    wizard = FullSetupWizard(
        prompter=_StubPrompter(),
        io=io,
        interactive=False,
        skip_apt=True,
        platform_override=_ubuntu("24.04"),
    )
    result = wizard.run()
    assert result.config_created is False
    # Existing content untouched.
    assert cfg.read_text(encoding="utf-8") == "agents: {}\n"


def test_wizard_macos_prints_brew_guidance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    io = _StubIO()
    macos = detect_platform(system="Darwin", python_version=(3, 12))
    wizard = FullSetupWizard(
        prompter=_StubPrompter(),
        io=io,
        interactive=False,
        skip_provider=True,
        platform_override=macos,
    )
    result = wizard.run()
    assert result.apt_attempted is False
    assert io.commands == []
    assert any("brew" in m for m in io.messages)


# ─── CLI integration ─────────────────────────────────────────────────


def test_setup_all_cli_print_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    from oxenclaw.cli.__main__ import app

    runner = CliRunner()
    result = runner.invoke(app, ["setup", "all", "--print-only", "--skip-provider"])
    assert result.exit_code == 0, result.output
    assert "one-shot full bootstrap" in result.output
    assert (tmp_path / "config.yaml").is_file()


def test_bare_setup_cli_runs_full_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`oxenclaw setup` with no subcommand runs the full wizard.

    CliRunner provides a non-tty stdin, so apt stays print-only and the
    provider step self-skips — the run completes without prompts.
    """
    monkeypatch.setenv("OXENCLAW_HOME", str(tmp_path))
    from oxenclaw.cli.__main__ import app

    runner = CliRunner()
    result = runner.invoke(app, ["setup"])
    assert result.exit_code == 0, result.output
    assert "Step 1/5" in result.output
    assert (tmp_path / "gateway-token").is_file()
