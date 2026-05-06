"""Opt-in installer for skill-declared binary dependencies.

Many ClawHub skills are knowledge-style: their `SKILL.md` teaches the LLM
to invoke a CLI binary that must already exist on PATH (e.g.
`yahoo-finance-cli` calls `yf` and `jq`). Skills declare those binaries
in `metadata.openclaw.requires.bins` and ship installation hints in
`metadata.openclaw.install`.

The base `SkillInstaller` (oxenclaw.clawhub.installer) deliberately does
*not* execute these install specs — auto-running brew/apt/npm at gateway
boot would be a serious security hole. This module exists for the case
where the user *explicitly* asks oxenClaw to run them: the
`oxenclaw skills install-bins <slug>` CLI walks the spec, prompts per
step, and runs only the steps the user confirms.

Supported `kind` values in v1: brew, apt, node/npm, pip, uv, go.
Refused kinds: `exec` (arbitrary shell), `download` (arbitrary URL).
On Linux when `brew` is not on PATH, `kind:brew` falls back to apt by
rewriting the argv to `apt-get install -y <formula>`. The fallback is
surfaced in the plan output so the user can decline.

Safety:
  * argv is built as a list; we never invoke a shell.
  * Package/formula values are validated against per-kind whitelist
    regexes before going into argv. Anything failing the regex is
    refused.
  * sudo is never auto-prepended. apt steps that need root will fail
    with permission denied; the user can re-run the CLI under sudo or
    execute the command manually.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Callable, Literal, Protocol

from oxenclaw.clawhub.frontmatter import SkillInstallSpec, SkillManifest
from oxenclaw.clawhub.loader import InstalledSkill, load_installed_skills
from oxenclaw.config.paths import OxenclawPaths, default_paths
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("clawhub.bin_installer")


# Per-kind safe-value regex. Mirrors openclaw's `assertSafeInstallerValue`.
_SAFE_BREW_FORMULA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@/-]*$")
_SAFE_APT_PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_SAFE_NPM_PACKAGE = re.compile(
    r"^(@[A-Za-z0-9][A-Za-z0-9._-]*\/)?[A-Za-z0-9][A-Za-z0-9._-]*$"
)
_SAFE_PIP_PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SAFE_GO_MODULE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class _Refused(Exception):
    """Spec is structurally invalid or value failed safety check."""


@dataclass(frozen=True)
class PlannedStep:
    """One install step ready (or refused) for execution."""

    index: int
    total: int
    spec: SkillInstallSpec
    decision: Literal["run", "skip"]
    argv: tuple[str, ...] | None
    reason: str
    effective_kind: str

    @property
    def label(self) -> str:
        return self.spec.label or self.spec.id or (self.spec.kind or "?")


@dataclass
class StepResult:
    step: PlannedStep
    confirmed: bool
    executed: bool
    exit_code: int | None
    stderr_tail: str | None


class Prompter(Protocol):
    def confirm(self, step: PlannedStep) -> bool: ...
    def notify(self, message: str) -> None: ...


def _on_path(name: str) -> bool:
    return shutil.which(name) is not None


def _check(value: str | None, pattern: re.Pattern[str], label: str) -> str:
    if not value:
        raise _Refused(f"missing {label}")
    if not pattern.match(value):
        raise _Refused(f"unsafe {label}: {value!r}")
    return value


def _build_argv(
    spec: SkillInstallSpec, *, host_os: str, brew_present: bool
) -> tuple[str, str, tuple[str, ...]]:
    """Return (effective_kind, human_summary, argv) or raise `_Refused`."""
    kind = (spec.kind or "").lower()
    if kind == "brew":
        formula = _check(
            spec.formula or spec.package, _SAFE_BREW_FORMULA, "brew formula"
        )
        if brew_present:
            return ("brew", f"brew install {formula}", ("brew", "install", formula))
        if host_os == "linux":
            return (
                "brew→apt-fallback",
                f"apt-get install -y {formula}  (brew not on PATH — using apt fallback)",
                ("apt-get", "install", "-y", formula),
            )
        raise _Refused(f"brew not on PATH and no fallback available for {host_os}")
    if kind == "apt":
        pkg = _check(spec.package or spec.formula, _SAFE_APT_PACKAGE, "apt package")
        return ("apt", f"apt-get install -y {pkg}", ("apt-get", "install", "-y", pkg))
    if kind in ("node", "npm"):
        pkg = _check(spec.package, _SAFE_NPM_PACKAGE, "npm package")
        return ("node", f"npm install -g {pkg}", ("npm", "install", "-g", pkg))
    if kind == "pip":
        pkg = _check(spec.package, _SAFE_PIP_PACKAGE, "pip package")
        return ("pip", f"pip install {pkg}", ("pip", "install", pkg))
    if kind == "uv":
        pkg = _check(spec.package, _SAFE_PIP_PACKAGE, "uv tool")
        return ("uv", f"uv tool install {pkg}", ("uv", "tool", "install", pkg))
    if kind == "go":
        module = _check(spec.module or spec.package, _SAFE_GO_MODULE, "go module")
        if "@" not in module:
            module = f"{module}@latest"
        return ("go", f"go install {module}", ("go", "install", module))
    if kind == "exec":
        raise _Refused("exec specs are not auto-run; install manually")
    if kind == "download":
        raise _Refused("download specs are not auto-run; install manually")
    raise _Refused(f"unknown install kind: {kind!r}")


def plan_install(
    manifest: SkillManifest,
    *,
    host_os: str | None = None,
    brew_present: bool | None = None,
) -> list[PlannedStep]:
    """Compute the per-step plan. `host_os`/`brew_present` are test seams."""
    os_id = (host_os or platform.system()).lower()
    brew_ok = brew_present if brew_present is not None else _on_path("brew")
    specs = list(manifest.openclaw.install)
    total = len(specs)
    out: list[PlannedStep] = []
    for i, spec in enumerate(specs, start=1):
        try:
            effective_kind, summary, argv = _build_argv(
                spec, host_os=os_id, brew_present=brew_ok
            )
            out.append(
                PlannedStep(
                    index=i,
                    total=total,
                    spec=spec,
                    decision="run",
                    argv=argv,
                    reason=summary,
                    effective_kind=effective_kind,
                )
            )
        except _Refused as exc:
            out.append(
                PlannedStep(
                    index=i,
                    total=total,
                    spec=spec,
                    decision="skip",
                    argv=None,
                    reason=str(exc),
                    effective_kind=spec.kind or "?",
                )
            )
    return out


Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(  # noqa: S603 — argv only, no shell
        list(argv),
        capture_output=True,
        text=True,
        check=False,
    )


def execute(
    plan: Iterable[PlannedStep],
    prompter: Prompter,
    *,
    dry_run: bool = False,
    runner: Runner | None = None,
) -> list[StepResult]:
    """Walk the plan, asking the prompter per runnable step.

    `runner` defaults to the module-level `_default_runner` resolved at
    call time so tests can `monkeypatch.setattr(...)` without having to
    thread a runner through the CLI.
    """
    if runner is None:
        runner = _default_runner
    results: list[StepResult] = []
    for step in plan:
        if step.decision == "skip":
            prompter.notify(
                f"[{step.index}/{step.total}] SKIP: {step.label} "
                f"(kind={step.effective_kind}; {step.reason})"
            )
            results.append(
                StepResult(
                    step=step,
                    confirmed=False,
                    executed=False,
                    exit_code=None,
                    stderr_tail=step.reason,
                )
            )
            continue
        prompter.notify(
            f"[{step.index}/{step.total}] {step.label} "
            f"(kind={step.effective_kind})\n      $ {step.reason}"
        )
        confirmed = prompter.confirm(step)
        if not confirmed or dry_run:
            results.append(
                StepResult(
                    step=step,
                    confirmed=confirmed,
                    executed=False,
                    exit_code=None,
                    stderr_tail="dry-run" if dry_run else "declined",
                )
            )
            continue
        assert step.argv is not None
        proc = runner(step.argv)
        tail = None
        if proc.returncode != 0 and proc.stderr:
            tail = "\n".join(proc.stderr.strip().splitlines()[-3:])
        results.append(
            StepResult(
                step=step,
                confirmed=True,
                executed=True,
                exit_code=proc.returncode,
                stderr_tail=tail,
            )
        )
    return results


def find_installed_skill(
    slug: str, paths: OxenclawPaths | None = None
) -> InstalledSkill | None:
    """Return the `InstalledSkill` matching `slug`, or None if not installed."""
    for s in load_installed_skills(paths or default_paths()):
        if s.slug == slug:
            return s
    return None
