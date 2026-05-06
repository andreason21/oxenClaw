"""Auto-approve policy for the assistant agent's `shell` tool.

When the assistant gets a `shell` tool registered (opt-in via
`OXENCLAW_ASSISTANT_SHELL=1`), every shell call would otherwise hit the
human-approval queue. That's the right default for mutating commands —
`rm -rf`, `git push`, `apt-get install` — but it's an awful UX for the
read-only CLIs that knowledge-style skills lean on (the
`yahoo-finance-cli` skill calls `yf quote 005930.KS`, an idempotent
data fetch). Bouncing every `yf` invocation through approval drowns the
operator in noise and discourages the model from using skills at all.

This module narrows the approval gate. A small set of CLI tools is
declared *read-only by name*; commands whose first token (or first two
tokens, for entries like `git status`) match the set bypass the gate
and execute directly. Everything else falls through to the regular
ApprovalManager flow.

Composition (constructor-time, frozen):
  * Built-in safe set: classic POSIX read-only utilities (cat, ls,
    head, tail, grep, awk, sort, …) plus `yf` and `jq` because
    yahoo-finance-cli is the canonical motivating skill.
  * Skill-declared bins: every `requires.bins` / `anyBins` entry from
    every installed skill is auto-added — installing a skill that
    documents a read-only CLI grants that CLI auto-approval without
    the operator editing config.
  * Operator additions: comma-separated `OXENCLAW_SHELL_WHITELIST`
    env var. Two-token entries are supported (`OXENCLAW_SHELL_WHITELIST
    ="git status,git log"`) so partially-mutating tools like `git`
    can be exposed only on their read subcommands.

Pipe support: a command like `yf quote AAPL | jq .price` chains two
read-only tools and is common in skill documentation. We accept
pipelines as long as *every* segment's leading binary is whitelisted.
A single non-whitelisted segment fails the whole command.

Refused outright (returns False before any whitelist match):
  * Sequencing: `;`, `&&`, `||`, line continuation
  * Redirection: `>`, `>>`, `<` (output capture is fine; redirection
    escapes the safe-CLI assumption)
  * Subshells: backtick, `$( … )`
  * shlex parse failure (unbalanced quotes, etc.)

The shell tool's own three-tier hardline/dangerous classifier still
runs after this layer; we only decide whether the call goes via the
approval queue or executes directly.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Iterable

# Read-only utilities that are safe to invoke unattended on a typical
# dev box. Mutating tools (`git`, `npm`, `apt-get`, `rm`, `cp`, `mv`,
# `chmod`, `chown`, …) are deliberately absent — operators who want
# them auto-approved must add them via the env var with the read-only
# subcommand spelled out (e.g. `git status`).
_BUILTIN_READONLY: frozenset[str] = frozenset(
    {
        # POSIX file readers
        "cat",
        "head",
        "tail",
        "less",
        "more",
        "wc",
        "file",
        "stat",
        # Filesystem listing (read-only)
        "ls",
        "find",
        "tree",
        "pwd",
        # Text processing (no in-place edits — those need `sed -i`)
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ripgrep",
        "awk",
        "sort",
        "uniq",
        "cut",
        "tr",
        "tee",  # only when reading; output-redir form covered by metachar check
        # Environment / identity
        "env",
        "printenv",
        "whoami",
        "id",
        "hostname",
        "uname",
        "date",
        # Echo helpers (the model sometimes uses these for templating)
        "echo",
        "printf",
        # Network probes (read-only)
        "ping",
        "nslookup",
        "dig",
        "host",
        # JSON / data utilities — the canonical pipe partner for
        # CLI skills like yahoo-finance-cli (`yf … | jq .field`).
        "jq",
        "yq",
        # The motivating skill's CLI.
        "yf",
    }
)

# Refuse if any of these appear anywhere in the command. We use a
# single-pass regex instead of multiple `in` checks so the cost stays
# constant for long commands.
#
# We intentionally do NOT carve out fd-duplication forms (`2>&1`) —
# even though those are technically benign, allowing `>` in any form
# is too easy to walk back from. Operators who legitimately need
# stderr captured can either pipe through `tee` (whitelisted) or
# accept the approval prompt for the rare case.
_DANGEROUS_METACHARS = re.compile(
    r"""
    ;             # statement separator
  | \|\|          # logical OR
  | &&            # logical AND
  | >             # any output redir (no carve-outs by design)
  | <             # any input redir
  | `             # legacy command substitution
  | \$\(          # modern command substitution
  | \\\n          # shell line continuation
  | \n            # multi-line script
    """,
    re.VERBOSE,
)


def _normalize_entry(entry: str) -> str:
    """Collapse internal whitespace so 'git  status' == 'git status'."""
    return " ".join(entry.split())


def build_shell_whitelist(
    *,
    skill_bins: Iterable[str] | None = None,
    env_extra: str | None = None,
    extra: Iterable[str] | None = None,
) -> frozenset[str]:
    """Compose the per-process auto-approve set.

    `skill_bins` is the union of `requires.bins` + `requires.any_bins`
    from every installed skill. The caller is responsible for
    extracting these (so this module avoids a clawhub import cycle).
    `env_extra` is read from the `OXENCLAW_SHELL_WHITELIST` env var by
    default — passing the resolved string lets tests inject without
    mutating os.environ. `extra` is for explicit programmatic
    additions, mainly tests.
    """
    out: set[str] = set(_BUILTIN_READONLY)
    if skill_bins:
        for b in skill_bins:
            if b and isinstance(b, str):
                out.add(_normalize_entry(b))
    raw_env = (
        env_extra
        if env_extra is not None
        else os.environ.get("OXENCLAW_SHELL_WHITELIST", "")
    )
    if raw_env.strip():
        for tok in raw_env.split(","):
            tok = _normalize_entry(tok)
            if tok:
                out.add(tok)
    if extra:
        for tok in extra:
            tok = _normalize_entry(tok or "")
            if tok:
                out.add(tok)
    return frozenset(out)


def _segment_first_match(tokens: list[str], whitelist: frozenset[str]) -> bool:
    """True iff the segment's leading binary (or 2-word prefix) is
    whitelisted."""
    if not tokens:
        return False
    if tokens[0] in whitelist:
        return True
    if len(tokens) >= 2 and f"{tokens[0]} {tokens[1]}" in whitelist:
        return True
    return False


def is_auto_approvable(command: str, whitelist: frozenset[str]) -> bool:
    """True when `command` can bypass the approval queue.

    Pure function so it's trivial to unit-test the policy without
    spinning up an ApprovalManager.
    """
    if not command or not command.strip():
        return False
    if _DANGEROUS_METACHARS.search(command):
        return False
    segments = [s.strip() for s in command.split("|")]
    if not all(segments):
        return False  # empty pipe segment ('| jq' or 'yf |')
    for seg in segments:
        try:
            tokens = shlex.split(seg)
        except ValueError:
            return False  # unbalanced quotes, etc.
        if not _segment_first_match(tokens, whitelist):
            return False
    return True


def assistant_shell_enabled() -> bool:
    """Feature flag for opting the default assistant agent into the
    `shell` tool. Off by default because shell access is a meaningful
    privilege escalation; operators must consciously turn it on."""
    raw = (os.environ.get("OXENCLAW_ASSISTANT_SHELL") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def gateway_bin_auto_install_enabled() -> bool:
    """Feature flag for letting the gateway run a skill's
    `metadata.openclaw.install` plan when an RPC client passes
    `with_bins=True` on `skills.install`.

    Off by default for the same reason as `installer.py:1-8`'s
    refusal to auto-run install specs — running brew/apt/npm from a
    daemon process is a meaningful privilege escalation. Operators
    who want one-click dashboard installs (skill files + binary
    dependencies) opt in by setting `OXENCLAW_GATEWAY_BIN_AUTO_INSTALL=1`.
    The CLI path (`oxenclaw skills install --yes`) is unaffected by
    this flag — that's a foreground command run with the operator's
    own shell credentials, not a daemon side-effect."""
    raw = (os.environ.get("OXENCLAW_GATEWAY_BIN_AUTO_INSTALL") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


__all__ = [
    "assistant_shell_enabled",
    "build_shell_whitelist",
    "gateway_bin_auto_install_enabled",
    "is_auto_approvable",
]
