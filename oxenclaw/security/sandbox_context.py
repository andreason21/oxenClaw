"""Workspace-level sandbox context.

`SandboxContext` is the per-run answer to: "where should this agent's
file operations land?" — the canonical workspace, a read-only mirror,
or a writable scratch copy.

Mirrors openclaw `resolveSandboxContext`. The TS version reads
container settings + sandbox plugins; ours assumes a single local
workspace dir and offers four modes:

  - **none**  — disabled. Tools see the canonical workspace as-is.
  - **ro**    — bind-mount-style reads from canonical, writes denied.
                File-ops tools must check `context.write_allowed`
                before touching disk.
  - **copy**  — workspace copied to a tmpdir on enter; agent mutations
                stay isolated. Useful for testing destructive code
                paths without dirtying the real tree.
  - **rw**    — full read+write directly on canonical (default
                behaviour for trusted operators).

The hard sandboxing for shell commands stays in
`oxenclaw.security.isolation` — this layer is for the file/edit/
process tool surface that the legacy port didn't gate.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("security.sandbox_context")

SandboxMode = Literal["none", "ro", "copy", "rw"]


@dataclass
class SandboxContext:
    """Resolved sandbox state for one run.

    `effective_workspace` is what tools should actually read/write to.
    `canonical_workspace` is the operator-visible directory the
    sandbox is shadowing. They differ only in `copy` mode.
    """

    mode: SandboxMode
    canonical_workspace: Path
    effective_workspace: Path
    write_allowed: bool

    @property
    def enabled(self) -> bool:
        return self.mode != "none"

    @property
    def is_copy(self) -> bool:
        return self.mode == "copy"

    def cleanup(self) -> None:
        """Remove a copy-mode scratch dir. Safe no-op for other modes."""
        if not self.is_copy:
            return
        if self.effective_workspace == self.canonical_workspace:
            return
        try:
            shutil.rmtree(self.effective_workspace)
        except OSError:
            logger.exception(
                "sandbox_context cleanup: failed to remove %s",
                self.effective_workspace,
            )


def resolve_sandbox_context(
    *,
    workspace_dir: Path | str,
    mode: SandboxMode = "none",
    copy_root: Path | str | None = None,
) -> SandboxContext:
    """Materialise a SandboxContext for the requested mode.

    `copy_root` (if provided) is the parent dir for `mode='copy'`
    scratch trees — defaults to the system tempdir. The scratch dir
    is created lazily; callers MUST invoke `context.cleanup()` when
    the run finishes.
    """
    canonical = Path(workspace_dir).resolve()
    if mode == "none":
        return SandboxContext(
            mode="none",
            canonical_workspace=canonical,
            effective_workspace=canonical,
            write_allowed=True,
        )
    if mode == "rw":
        return SandboxContext(
            mode="rw",
            canonical_workspace=canonical,
            effective_workspace=canonical,
            write_allowed=True,
        )
    if mode == "ro":
        return SandboxContext(
            mode="ro",
            canonical_workspace=canonical,
            effective_workspace=canonical,
            write_allowed=False,
        )
    if mode == "copy":
        parent = Path(copy_root) if copy_root else Path(tempfile.gettempdir())
        parent.mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="oxenclaw-sandbox-", dir=str(parent)))
        if canonical.is_dir():
            # Copy contents into the scratch dir.
            for child in canonical.iterdir():
                target = scratch / child.name
                if child.is_dir():
                    shutil.copytree(child, target, symlinks=False)
                else:
                    shutil.copy2(child, target)
        return SandboxContext(
            mode="copy",
            canonical_workspace=canonical,
            effective_workspace=scratch,
            write_allowed=True,
        )
    raise ValueError(f"unknown sandbox mode: {mode!r}")


__all__ = [
    "SandboxContext",
    "SandboxMode",
    "resolve_sandbox_context",
]
