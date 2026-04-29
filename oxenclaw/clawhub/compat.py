"""Compatibility checker for skill manifests.

A skill that requires macOS-only binaries, an absent CLI, or an
unset API key env var will fail at install or first use. The
``skills.search`` / ``skills.list_remote`` / ``skills.list_installed``
RPCs use this module to annotate every result with a
``compat: {installable, reasons, ...}`` block, and (by default) to
hide entries that obviously can't run in the current environment.

Sources of incompatibility we recognise:

  * ``openclaw.os: ["darwin"]`` and current platform isn't macOS
    (Linux/Windows/etc.) — this is the most common false-positive
    source operators hit (catalog full of mac-only stock-quote skills
    when they're on Linux).
  * ``openclaw.requires.bins: ["foo", "bar"]`` — every binary must
    be on PATH; missing ones are listed individually.
  * ``openclaw.requires.any_bins: ["python3", "python"]`` — at
    least one must be on PATH; report all when none satisfy.
  * ``openclaw.requires.env: ["OPENAI_API_KEY"]`` — every env var
    must be set to a non-empty value.

The per-skill manifest may also live under ``manifest.openclaw.…`` or
``latestVersion.manifest.openclaw.…`` depending on the registry
endpoint shape; ``check_skill_dict_compatibility`` walks both.

Inputs that are missing entirely (search summary with no manifest)
report ``installable=True`` because we can't prove otherwise — better
to show a maybe-OK skill than to hide every catalog entry that
doesn't ship full metadata in its summary.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CompatibilityReport:
    installable: bool = True
    reasons: list[str] = field(default_factory=list)
    missing_bins: list[str] = field(default_factory=list)
    missing_any_bins: list[str] = field(default_factory=list)
    missing_env: list[str] = field(default_factory=list)
    unsupported_os: bool = False
    # The platform name we actually checked against — useful for the
    # dashboard tooltip ("hidden because skill is darwin-only and you
    # are on linux").
    current_os: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "installable": self.installable,
            "reasons": list(self.reasons),
            "missing_bins": list(self.missing_bins),
            "missing_any_bins": list(self.missing_any_bins),
            "missing_env": list(self.missing_env),
            "unsupported_os": self.unsupported_os,
            "current_os": self.current_os,
        }


# Skill manifests tend to use either Python's `sys.platform` strings
# (`linux`, `darwin`, `win32`) or human names (`linux`, `macos`,
# `windows`). Normalise both directions.
_OS_SYNONYMS: dict[str, set[str]] = {
    "linux": {"linux"},
    "darwin": {"darwin", "macos", "mac", "osx"},
    "win32": {"win32", "windows", "win"},
}


def _current_os_name() -> str:
    """Return one of `linux` / `darwin` / `win32` for synonym lookup."""
    p = sys.platform
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "darwin"
    if p.startswith("win"):
        return "win32"
    return p


def _matches_current_os(declared: Iterable[str], current: str) -> bool:
    """True when at least one entry of `declared` is recognised as
    naming the same OS as `current`. Empty declared list = no OS
    constraint = always matches."""
    declared_norm = {d.strip().lower() for d in declared if d and d.strip()}
    if not declared_norm:
        return True
    synonyms = _OS_SYNONYMS.get(current, {current})
    return any(d in synonyms for d in declared_norm)


def check_compatibility(
    *,
    os_list: Iterable[str] = (),
    requires_bins: Iterable[str] = (),
    requires_any_bins: Iterable[str] = (),
    requires_env: Iterable[str] = (),
    current_os: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
) -> CompatibilityReport:
    """Run every check and accumulate the reasons. Returns a report
    that's safe to render in the UI verbatim."""
    cur = current_os or _current_os_name()
    env = environ if environ is not None else os.environ
    report = CompatibilityReport(current_os=cur)

    if not _matches_current_os(os_list, cur):
        report.unsupported_os = True
        report.installable = False
        declared = ", ".join(sorted({o for o in os_list if o.strip()}))
        report.reasons.append(
            f"requires {declared or '(unspecified)'}; current platform is {cur}"
        )

    missing_bins = [b for b in requires_bins if b and not which(b)]
    if missing_bins:
        report.missing_bins.extend(missing_bins)
        report.installable = False
        report.reasons.append(
            "missing required binaries on PATH: " + ", ".join(missing_bins)
        )

    any_bins = [b for b in requires_any_bins if b]
    if any_bins and not any(which(b) for b in any_bins):
        report.missing_any_bins.extend(any_bins)
        report.installable = False
        report.reasons.append(
            "no candidate binary on PATH (need at least one of: "
            + ", ".join(any_bins)
            + ")"
        )

    missing_env = [v for v in requires_env if v and not env.get(v)]
    if missing_env:
        report.missing_env.extend(missing_env)
        report.installable = False
        report.reasons.append(
            "missing required env vars: " + ", ".join(missing_env)
        )

    return report


def _metadata_oc_block(d: dict[str, Any]) -> dict[str, Any] | None:
    """Pick `metadata.openclaw` or `metadata.clawdbot` (Clawdbot-
    published ClawHub skills use the latter). Returns the inner dict
    or None when neither is present."""
    md = d.get("metadata")
    if not isinstance(md, dict):
        return None
    for key in ("openclaw", "clawdbot"):
        block = md.get(key)
        if isinstance(block, dict):
            return block
    return None


def _walk_manifest(skill: dict[str, Any]) -> dict[str, Any] | None:
    """Locate the SKILL.md frontmatter object inside a clawhub
    payload. Endpoint shapes vary:

      - search/list summary: top-level `requires`/`os` if present
      - detail: `manifest.openclaw.{os,requires}`
      - detail (versioned): `latestVersion.manifest.openclaw.{...}`

    Each level supports both `metadata.openclaw` and
    `metadata.clawdbot` because real publishers use either name.

    Returns the dict that holds `os` / `requires`, or None when no
    constraint metadata is present (treat as "compatible by default").
    """
    if not isinstance(skill, dict):
        return None
    # Versioned detail.
    lv = skill.get("latestVersion")
    if isinstance(lv, dict):
        m = lv.get("manifest")
        if isinstance(m, dict):
            oc = m.get("openclaw") or _metadata_oc_block(m)
            if isinstance(oc, dict):
                return oc
    # Plain detail.
    m = skill.get("manifest")
    if isinstance(m, dict):
        oc = m.get("openclaw") or _metadata_oc_block(m)
        if isinstance(oc, dict):
            return oc
    # Already-flattened openclaw block.
    oc = skill.get("openclaw")
    if isinstance(oc, dict):
        return oc
    # Top-level `metadata.openclaw` / `metadata.clawdbot`.
    block = _metadata_oc_block(skill)
    if block is not None:
        return block
    # Last resort: top-level `requires`/`os` on the summary.
    if "requires" in skill or "os" in skill:
        return skill
    return None


def check_skill_dict_compatibility(
    skill: dict[str, Any],
    *,
    current_os: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
) -> CompatibilityReport:
    """Convenience for clawhub payloads: pulls `os` + `requires.*`
    out of whatever manifest shape the endpoint returns and runs
    ``check_compatibility``. Empty / missing metadata → installable
    by default (no false-negative hides on summary results)."""
    block = _walk_manifest(skill) or {}
    os_list = block.get("os") or []
    requires = block.get("requires") or {}
    if not isinstance(requires, dict):
        requires = {}
    bins = requires.get("bins") or []
    any_bins = requires.get("anyBins") or requires.get("any_bins") or []
    env = requires.get("env") or []
    return check_compatibility(
        os_list=os_list if isinstance(os_list, list) else [],
        requires_bins=bins if isinstance(bins, list) else [],
        requires_any_bins=any_bins if isinstance(any_bins, list) else [],
        requires_env=env if isinstance(env, list) else [],
        current_os=current_os,
        which=which,
        environ=environ,
    )


__all__ = [
    "CompatibilityReport",
    "check_compatibility",
    "check_skill_dict_compatibility",
]
