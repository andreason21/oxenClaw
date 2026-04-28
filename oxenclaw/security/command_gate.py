"""Three-tier command gate for the shell tool.

Layers, in order:

1. **HARDLINE** — unconditional refusal. Matches catastrophic patterns
   (``rm -rf /``, ``mkfs``, ``dd if=… of=/dev/sd…``, fork bomb,
   ``kill -1``, ``shutdown``/``reboot``, ``> /dev/sda``,
   ``chmod -R 777 /``, piping ``curl|bash`` and ``eval "$(curl …"``).
   Even with a YOLO flag set, hardline is unconditional.

2. **DANGEROUS** — recoverable-but-costly. Soft block needing per-
   session approval. Examples: ``rm -rf <user_path>``,
   ``git push --force``, ``git reset --hard``, ``dd``, broad
   ``chmod -R``, ``npm publish``, ``pip install --user``, anything
   piped through sudo, ``>> ~/.bashrc``.

3. **OK** — passes through to subprocess.

The patterns are anchored to a "command position" prefix (``_CMDPOS``)
that matches the start of a command (start-of-string, after
``;|&&|||\\n``, after ``sudo``, after ``env VAR=val``, after ``exec``)
so ``echo "rm -rf /"`` and ``grep "shutdown" log`` do not false-positive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

Verdict = Literal["hardline", "dangerous", "ok"]


# Anchor the start of a command (start of string, after a separator,
# possibly preceded by sudo/env/exec wrappers).
_CMDPOS = (
    r"(?:^|[;&|\n`]|\$\()"  # start position
    r"\s*"  # optional whitespace
    r"(?:sudo\s+(?:-[^\s]+\s+)*)?"  # optional sudo with flags
    r"(?:env\s+(?:\w+=\S*\s+)*)?"  # optional env VAR=VAL ...
    r"(?:(?:exec|nohup|setsid|time)\s+)*"
    r"\s*"
)


# Compiled (regex, label) pairs.
HARDLINE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # rm -rf rooted at / or sensitive prefixes
    (
        re.compile(
            _CMDPOS + r"rm\s+(?:-[a-zA-Z]*\s+)*-?[a-zA-Z]*r[a-zA-Z]*f?[^\s]*\s+/(?:\s|$)",
            re.IGNORECASE,
        ),
        "rm -rf of root filesystem",
    ),
    (
        re.compile(
            _CMDPOS
            + r"rm\s+(?:-[a-zA-Z]*\s+)*-?[a-zA-Z]*r[a-zA-Z]*f?[^\s]*\s+/(?:etc|var|usr|bin|sbin|boot|lib|lib64|root|home)(?:/|\s|$)",
            re.IGNORECASE,
        ),
        "rm -rf of system directory",
    ),
    (
        re.compile(
            _CMDPOS
            + r"rm\s+(?:-[a-zA-Z]*\s+)*-?[a-zA-Z]*r[a-zA-Z]*f?[^\s]*\s+(?:~|\$HOME)(?:/|\s|$)",
            re.IGNORECASE,
        ),
        "rm -rf of home directory",
    ),
    # mkfs
    (re.compile(_CMDPOS + r"mkfs(?:\.[a-z0-9]+)?\b", re.IGNORECASE), "mkfs (format filesystem)"),
    # dd to raw block device
    (
        re.compile(r"\bdd\b[^\n]*\bof=/dev/(?:sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*", re.IGNORECASE),
        "dd to raw block device",
    ),
    # > /dev/sd…
    (
        re.compile(r">\s*/dev/(?:sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*\b", re.IGNORECASE),
        "redirect to raw block device",
    ),
    # Fork bomb
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    # kill -1 / kill -9 -1
    (re.compile(_CMDPOS + r"kill\s+(?:-[^\s]+\s+)*-1\b", re.IGNORECASE), "kill all processes"),
    # shutdown / reboot / halt / poweroff at command position
    (
        re.compile(_CMDPOS + r"(?:shutdown|reboot|halt|poweroff)\b", re.IGNORECASE),
        "system shutdown/reboot",
    ),
    (re.compile(_CMDPOS + r"init\s+[06]\b", re.IGNORECASE), "init 0/6"),
    (
        re.compile(_CMDPOS + r"systemctl\s+(?:poweroff|reboot|halt|kexec)\b", re.IGNORECASE),
        "systemctl poweroff/reboot",
    ),
    # chmod -R 777 /
    (
        re.compile(
            _CMDPOS + r"chmod\s+(?:-[^\s]*\s+)*-?[^\s]*R[^\s]*\s+777\s+/(?:\s|$)", re.IGNORECASE
        ),
        "chmod -R 777 of root",
    ),
    # curl|sh / wget|bash
    (
        re.compile(r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:ba)?sh\b", re.IGNORECASE),
        "curl|sh remote execution",
    ),
    # eval "$(curl …)"
    (
        re.compile(r"\beval\s+[\"']?\$\(\s*(?:curl|wget)\b", re.IGNORECASE),
        "eval $(curl ...) remote execution",
    ),
]


DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # rm -rf of any path (non-root, non-system) — caught here as dangerous.
    (
        re.compile(_CMDPOS + r"rm\s+(?:-[a-zA-Z]*\s+)*-?[a-zA-Z]*r[a-zA-Z]*f", re.IGNORECASE),
        "rm -rf of user path",
    ),
    (
        re.compile(_CMDPOS + r"rm\s+(?:-[a-zA-Z]*\s+)*--recursive\b", re.IGNORECASE),
        "rm --recursive",
    ),
    # git destructive
    (re.compile(r"\bgit\s+push\b.*--force\b", re.IGNORECASE), "git push --force"),
    (re.compile(r"\bgit\s+push\b.*\s-f(?:\s|$)", re.IGNORECASE), "git push -f"),
    (re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE), "git reset --hard"),
    (re.compile(r"\bgit\s+clean\s+-[^\s]*f", re.IGNORECASE), "git clean -f"),
    (re.compile(r"\bgit\s+branch\s+-D\b"), "git branch -D"),
    # dd in any form
    (re.compile(_CMDPOS + r"dd\s+.*\bif=", re.IGNORECASE), "dd disk copy"),
    # broad chmod -R
    (
        re.compile(_CMDPOS + r"chmod\s+(?:-[^\s]*\s+)*-?[^\s]*R", re.IGNORECASE),
        "chmod -R (broad recursion)",
    ),
    # npm publish, pip install --user
    (re.compile(_CMDPOS + r"npm\s+publish\b", re.IGNORECASE), "npm publish"),
    (
        re.compile(_CMDPOS + r"pip\s+install\s+(?:[^\n]*\s)?--user\b", re.IGNORECASE),
        "pip install --user",
    ),
    # piping through sudo
    (re.compile(r"\|\s*sudo\b", re.IGNORECASE), "pipe into sudo"),
    # appending to shell rc / profile files
    (
        re.compile(
            r">>\s*[\"']?(?:~|\$HOME)?/?\.(?:bashrc|zshrc|profile|bash_profile|zshenv|zprofile)\b",
            re.IGNORECASE,
        ),
        "append to shell rc file",
    ),
    # mkfs / overwriting block devices were caught by hardline; xargs+rm and find -exec rm are still risky
    (re.compile(r"\bxargs\s+.*\brm\b", re.IGNORECASE), "xargs with rm"),
    (re.compile(r"\bfind\b.*-exec\s+(?:/\S*/)?rm\b", re.IGNORECASE), "find -exec rm"),
    (re.compile(r"\bfind\b.*-delete\b", re.IGNORECASE), "find -delete"),
]


def detect_command_threats(cmd: str) -> tuple[Verdict, str | None]:
    """Classify ``cmd`` against hardline and dangerous patterns.

    Returns ``(verdict, label)`` where ``label`` is the human-readable
    pattern description for hardline / dangerous matches and ``None``
    when the verdict is ``"ok"``.
    """
    for pattern, label in HARDLINE_PATTERNS:
        if pattern.search(cmd):
            return ("hardline", label)
    for pattern, label in DANGEROUS_PATTERNS:
        if pattern.search(cmd):
            return ("dangerous", label)
    return ("ok", None)


@dataclass
class CommandGate:
    """Per-session approval state for the shell command gate."""

    _approved: dict[str, set[str]] = field(default_factory=dict)
    yolo_session: set[str] = field(default_factory=set)

    def is_session_approved(self, session_key: str, pattern_label: str) -> bool:
        return pattern_label in self._approved.get(session_key, set())

    def approve_session(self, session_key: str, pattern_label: str) -> None:
        self._approved.setdefault(session_key, set()).add(pattern_label)

    def enable_yolo(self, session_key: str) -> None:
        self.yolo_session.add(session_key)

    def disable_yolo(self, session_key: str) -> None:
        self.yolo_session.discard(session_key)

    def is_yolo(self, session_key: str) -> bool:
        return session_key in self.yolo_session

    def clear(self, session_key: str | None = None) -> None:
        if session_key is None:
            self._approved.clear()
            self.yolo_session.clear()
        else:
            self._approved.pop(session_key, None)
            self.yolo_session.discard(session_key)


__all__ = [
    "DANGEROUS_PATTERNS",
    "HARDLINE_PATTERNS",
    "CommandGate",
    "Verdict",
    "detect_command_threats",
]
