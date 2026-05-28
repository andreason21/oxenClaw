"""Platform detection + system-dependency planning for `oxenclaw setup`.

Pure, side-effect-free helpers consumed by the one-shot `FullSetupWizard`
(`oxenclaw.flows.full_setup`). Kept separate so the version-specific apt
logic can be unit-tested without touching the real machine: every input
(`/etc/os-release` text, uname release, running Python version) is
injectable.

The package list mirrors what `docs/INSTALL_WSL.md` documents by hand:

- **build-essential / git / cmake / pkg-config** — compiling the
  `llamacpp-direct` backend and native wheels.
- **bubblewrap** — provides `bwrap`, the preferred sandbox-isolation
  backend (the doctor warns when it's missing).
- **curl / ca-certificates** — `ollama` install script + `hf download`.
- **Python**: Ubuntu 22.04 ships 3.10, below oxenClaw's 3.11 floor, so we
  add the deadsnakes PPA and install the 3.12 stack. 24.04+ ships 3.12
  natively, so the distro `python3-venv` / `python3-dev` packages suffice.
"""

from __future__ import annotations

import platform as _platform
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ─── Platform info ────────────────────────────────────────────────────


@dataclass(frozen=True)
class PlatformInfo:
    """Resolved host description driving the dependency plan."""

    system: str  # "Linux" | "Darwin" | "Windows"
    distro_id: str | None  # "ubuntu" | "debian" | ...  (os-release ID)
    distro_like: str | None  # os-release ID_LIKE, e.g. "debian"
    version_id: str | None  # "24.04"
    codename: str | None  # "noble"
    is_wsl: bool
    python_version: tuple[int, int]  # (3, 12)

    @property
    def is_ubuntu(self) -> bool:
        return self.distro_id == "ubuntu"

    @property
    def is_debian_like(self) -> bool:
        """True when `apt-get` is the right package manager."""
        if self.system != "Linux":
            return False
        return self.distro_id in {"ubuntu", "debian", "linuxmint", "pop"} or (
            self.distro_like is not None and "debian" in self.distro_like
        )

    @property
    def python_ok(self) -> bool:
        """True when the running interpreter already meets the 3.11 floor."""
        return self.python_version >= (3, 11)

    @property
    def pretty(self) -> str:
        bits: list[str] = []
        if self.distro_id and self.version_id:
            bits.append(f"{self.distro_id} {self.version_id}")
        elif self.system:
            bits.append(self.system)
        if self.codename:
            bits.append(f"({self.codename})")
        if self.is_wsl:
            bits.append("[WSL2]")
        py = ".".join(str(n) for n in self.python_version)
        bits.append(f"python {py}")
        return " ".join(bits)


def _parse_os_release(text: str) -> dict[str, str]:
    """Parse `/etc/os-release` `KEY=value` lines into a dict.

    Values may be quoted (`NAME="Ubuntu"`); strip a single layer of
    surrounding single/double quotes. Malformed lines are skipped rather
    than raising — this runs on the setup hot path.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def _read_os_release_text() -> str | None:
    for candidate in (Path("/etc/os-release"), Path("/usr/lib/os-release")):
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return None


def _detect_wsl(uname_release: str, *, allow_proc_fallback: bool = True) -> bool:
    release = uname_release.lower()
    if "microsoft" in release or "wsl" in release:
        return True
    # The /proc fallback covers containers that copy /etc/os-release but
    # report a clean uname. Skip it when the caller injected an explicit
    # `uname_release` (tests) — that value is then authoritative.
    if not allow_proc_fallback:
        return False
    osrelease = Path("/proc/sys/kernel/osrelease")
    try:
        if osrelease.exists():
            text = osrelease.read_text(errors="replace").lower()
            return "microsoft" in text or "wsl" in text
    except OSError:
        pass
    return False


def detect_platform(
    *,
    os_release_text: str | None = None,
    system: str | None = None,
    uname_release: str | None = None,
    python_version: tuple[int, int] | None = None,
) -> PlatformInfo:
    """Resolve a `PlatformInfo` for the current host.

    Every input is injectable so tests can simulate a 22.04 box from a
    24.04 host (and vice versa) without mocks. Unset args fall through to
    the real environment.
    """
    sysname = system if system is not None else _platform.system()
    release_injected = uname_release is not None
    release = uname_release if uname_release is not None else _platform.release()
    pyver = python_version if python_version is not None else (sys.version_info[0], sys.version_info[1])

    distro_id: str | None = None
    distro_like: str | None = None
    version_id: str | None = None
    codename: str | None = None

    if sysname == "Linux":
        text = os_release_text if os_release_text is not None else _read_os_release_text()
        if text:
            data = _parse_os_release(text)
            distro_id = (data.get("ID") or "").lower() or None
            distro_like = (data.get("ID_LIKE") or "").lower() or None
            version_id = data.get("VERSION_ID") or None
            codename = (data.get("VERSION_CODENAME") or "").lower() or None

    is_wsl = sysname == "Linux" and _detect_wsl(
        release, allow_proc_fallback=not release_injected
    )

    return PlatformInfo(
        system=sysname,
        distro_id=distro_id,
        distro_like=distro_like,
        version_id=version_id,
        codename=codename,
        is_wsl=is_wsl,
        python_version=pyver,
    )


# ─── apt plan ─────────────────────────────────────────────────────────

# Packages oxenClaw needs regardless of Ubuntu version, on any
# debian-like distro. `bubblewrap` ships the `bwrap` binary the sandbox
# isolation backend prefers; the rest are the llama.cpp build toolchain.
_BASE_APT_PACKAGES: tuple[str, ...] = (
    "build-essential",
    "git",
    "cmake",
    "pkg-config",
    "curl",
    "ca-certificates",
    "bubblewrap",
)

# Ubuntu 22.04 ships Python 3.10 (< the 3.11 floor) — pull 3.12 from the
# deadsnakes PPA. This is the exact recipe in docs/INSTALL_WSL.md §3.
_DEADSNAKES_PYTHON: tuple[str, ...] = (
    "python3.12",
    "python3.12-venv",
    "python3.12-dev",
)

# 24.04+ (and unknown debian-likes): distro python3 is already 3.11+.
_NATIVE_PYTHON: tuple[str, ...] = (
    "python3-venv",
    "python3-dev",
)


@dataclass(frozen=True)
class AptPlan:
    """The version-specific apt work needed to satisfy oxenClaw."""

    packages: list[str]
    # Extra commands that must run *before* the install (e.g. adding the
    # deadsnakes PPA). Each is a bare argv WITHOUT a leading `sudo`.
    pre_commands: list[list[str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def command_sequence(self, *, use_sudo: bool) -> list[list[str]]:
        """Full ordered argv sequence: update → pre-commands → install.

        `use_sudo` prepends `sudo` to every command (skip it when already
        running as root). The list is render-only — the wizard decides
        whether to execute or just print it.
        """
        prefix = ["sudo"] if use_sudo else []
        seq: list[list[str]] = [[*prefix, "apt-get", "update"]]
        for cmd in self.pre_commands:
            seq.append([*prefix, *cmd])
            # add-apt-repository changes the source list; refresh after it.
            if cmd and cmd[0] == "add-apt-repository":
                seq.append([*prefix, "apt-get", "update"])
        seq.append([*prefix, "apt-get", "install", "-y", *self.packages])
        return seq


def apt_plan_for(plat: PlatformInfo) -> AptPlan:
    """Compute the apt package plan for a debian-like host.

    Callers should gate on `plat.is_debian_like` first — for non-apt
    systems (macOS / unknown) use `non_apt_guidance` instead.
    """
    packages = list(_BASE_APT_PACKAGES)
    pre_commands: list[list[str]] = []
    notes: list[str] = []

    if plat.version_id == "22.04":
        pre_commands = [
            ["apt-get", "install", "-y", "software-properties-common"],
            ["add-apt-repository", "-y", "ppa:deadsnakes/ppa"],
        ]
        packages.extend(_DEADSNAKES_PYTHON)
        notes.append(
            "Ubuntu 22.04 ships Python 3.10 (below the 3.11 floor) — "
            "installing python3.12 via the deadsnakes PPA."
        )
    else:
        packages.extend(_NATIVE_PYTHON)
        if not plat.is_ubuntu:
            notes.append(
                "Non-Ubuntu apt distro — using generic python3-venv/python3-dev. "
                "Ensure your python3 is >= 3.11."
            )

    notes.append(
        "Browser tools (optional) also need: "
        "`playwright install chromium && sudo playwright install-deps chromium`."
    )
    return AptPlan(packages=packages, pre_commands=pre_commands, notes=notes)


def non_apt_guidance(plat: PlatformInfo) -> list[str]:
    """Human-readable install hints for hosts without apt (macOS, etc.)."""
    if plat.system == "Darwin":
        return [
            "macOS detected — install the toolchain with Homebrew:",
            "  xcode-select --install        # build-essential equivalent",
            "  brew install cmake git pkg-config",
            "  (llamacpp-direct uses Metal automatically; no sandbox pkg needed)",
        ]
    if plat.system == "Windows":
        return [
            "Native Windows is unsupported — use WSL2 (see docs/INSTALL_WSL.md).",
        ]
    return [
        f"No apt on this host ({plat.pretty}). Install the equivalents of: "
        f"{', '.join(_BASE_APT_PACKAGES)} plus a Python 3.11+ venv toolchain "
        "using your distro's package manager.",
    ]


__all__ = [
    "AptPlan",
    "PlatformInfo",
    "apt_plan_for",
    "detect_platform",
    "non_apt_guidance",
]
