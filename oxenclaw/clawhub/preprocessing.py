"""Skill body template variable substitution.

Mirrors `hermes-agent/agent/skill_preprocessing.py:13-131`. Replaces
`${OXENCLAW_SKILL_DIR}` and `${OXENCLAW_SESSION_ID}` placeholders in a
skill's markdown body before it's handed to the model. An optional
inline-shell escape (`` !`cmd` ``) is supported but disabled by
default because letting an arbitrary skill execute shell at activation
time is unsafe — operators flip the env flag
`OXENCLAW_ALLOW_SKILL_INLINE_SHELL=1` to opt in.

Caps mirror hermes:
- 4000-char limit on inline-shell output (truncated with notice).
- 5-second timeout on each `!cmd` invocation.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_SHELL_INLINE_RE = re.compile(r"!`([^`]+)`")
_MAX_OUTPUT_CHARS = 4000
_SHELL_TIMEOUT_S = 5.0


def _allow_inline_shell() -> bool:
    raw = os.environ.get("OXENCLAW_ALLOW_SKILL_INLINE_SHELL", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _run_inline_shell(cmd: str) -> str:
    try:
        proc = subprocess.run(  # noqa: S603 — operator-opt-in
            cmd,
            shell=True,  # noqa: S602
            capture_output=True,
            text=True,
            timeout=_SHELL_TIMEOUT_S,
        )
        out = proc.stdout or proc.stderr or ""
    except subprocess.TimeoutExpired:
        return f"[inline shell timeout after {_SHELL_TIMEOUT_S:.0f}s: {cmd}]"
    except Exception as exc:  # noqa: BLE001
        logger.debug("inline shell %s raised: %s", cmd, exc)
        return f"[inline shell failed: {exc}]"
    if len(out) > _MAX_OUTPUT_CHARS:
        return out[:_MAX_OUTPUT_CHARS] + "\n[...truncated]"
    return out


def preprocess_skill_body(
    body: str,
    *,
    skill_dir: Path,
    session_id: str = "",
) -> str:
    """Substitute template variables in `body`.

    Always-on substitutions:
      ${OXENCLAW_SKILL_DIR}    → absolute skill directory
      ${OXENCLAW_SESSION_ID}   → session id (empty string if unknown)

    Opt-in (env flag) substitutions:
      ` !`cmd` `               → captured stdout of `cmd`
    """
    out = body
    out = out.replace("${OXENCLAW_SKILL_DIR}", str(skill_dir))
    out = out.replace("${OXENCLAW_SESSION_ID}", session_id or "")

    if _allow_inline_shell():
        def _sub(match: re.Match[str]) -> str:
            cmd = match.group(1).strip()
            if not cmd:
                return ""
            return _run_inline_shell(cmd).rstrip()

        out = _SHELL_INLINE_RE.sub(_sub, out)
    return out


__all__ = ["preprocess_skill_body"]
