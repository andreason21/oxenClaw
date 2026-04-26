#!/usr/bin/env python3
"""Bump the project version across the three files that hold it.

sampyClaw stores its version in three places that must stay in sync:

- `pyproject.toml`            — Python package version (PyPI)
- `desktop/src-tauri/Cargo.toml` — Rust crate version
- `desktop/src-tauri/tauri.conf.json` — Tauri bundle / .msi product version

This script accepts the new version as the sole positional argument,
validates the format (PEP 440 / SemVer 2 numeric subset), updates all
three files in place, and prints a diff summary. It is also used by
`scripts/check_versions.py` (and the release workflow) to verify the
three files agree before publishing.

Usage:
    python scripts/bump_version.py 0.2.0
    python scripts/bump_version.py --check         # exit 1 if out of sync
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

PYPROJECT = REPO / "pyproject.toml"
CARGO = REPO / "desktop" / "src-tauri" / "Cargo.toml"
TAURI_CONF = REPO / "desktop" / "src-tauri" / "tauri.conf.json"

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][\w.]+)?$")


def read_pyproject() -> str:
    text = PYPROJECT.read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    if not m:
        raise SystemExit(f"could not find version in {PYPROJECT}")
    return m.group(1)


def read_cargo() -> str:
    text = CARGO.read_text()
    # The first `version = "..."` after `[package]` is the crate version.
    m = re.search(
        r'^\[package\][^\[]*?^version\s*=\s*"([^"]+)"',
        text, flags=re.MULTILINE | re.DOTALL,
    )
    if not m:
        raise SystemExit(f"could not find version in {CARGO}")
    return m.group(1)


def read_tauri_conf() -> str:
    data = json.loads(TAURI_CONF.read_text())
    if "version" not in data:
        raise SystemExit(f"no version field in {TAURI_CONF}")
    return data["version"]


def write_pyproject(new: str) -> None:
    text = PYPROJECT.read_text()
    text = re.sub(
        r'^(version\s*=\s*)"[^"]+"',
        rf'\g<1>"{new}"',
        text, count=1, flags=re.MULTILINE,
    )
    PYPROJECT.write_text(text)


def write_cargo(new: str) -> None:
    text = CARGO.read_text()
    # Replace the FIRST `version = "..."` after `[package]`. We do this
    # by splitting on `[package]` and rewriting only the head section.
    head, sep, tail = text.partition("[package]")
    if not sep:
        raise SystemExit("Cargo.toml missing [package] section")
    # `tail` starts with the package section's contents; rewrite up to
    # the next section header.
    section, next_sep, rest = tail.partition("\n[")
    section = re.sub(
        r'^(version\s*=\s*)"[^"]+"',
        rf'\g<1>"{new}"',
        section, count=1, flags=re.MULTILINE,
    )
    CARGO.write_text(head + sep + section + next_sep + rest)


def write_tauri_conf(new: str) -> None:
    data = json.loads(TAURI_CONF.read_text())
    data["version"] = new
    # Preserve the trailing newline + 2-space indent the file uses.
    TAURI_CONF.write_text(json.dumps(data, indent=2) + "\n")


def all_versions() -> dict[str, str]:
    return {
        "pyproject.toml": read_pyproject(),
        "Cargo.toml": read_cargo(),
        "tauri.conf.json": read_tauri_conf(),
    }


def check() -> int:
    versions = all_versions()
    distinct = set(versions.values())
    if len(distinct) == 1:
        print(f"ok — all three files agree on version {distinct.pop()}")
        return 0
    print("VERSION MISMATCH:")
    for name, ver in versions.items():
        print(f"  {name:24s} {ver}")
    return 1


def bump(new: str) -> None:
    if not VERSION_RE.match(new):
        raise SystemExit(
            f"invalid version {new!r} — expected MAJOR.MINOR.PATCH "
            f"(optional `-pre` / `+build` suffix)"
        )
    before = all_versions()
    write_pyproject(new)
    write_cargo(new)
    write_tauri_conf(new)
    after = all_versions()
    print("bumped:")
    for name in before:
        print(f"  {name:24s} {before[name]} -> {after[name]}")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("version", nargs="?", help="new MAJOR.MINOR.PATCH version")
    p.add_argument("--check", action="store_true",
                   help="verify all three files agree; exit 1 if not")
    args = p.parse_args(argv)
    if args.check:
        return check()
    if not args.version:
        p.error("version is required (or use --check)")
    bump(args.version)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
