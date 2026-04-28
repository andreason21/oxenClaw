"""SandboxContext: workspace-level isolation for the file-tool surface."""

from __future__ import annotations

from pathlib import Path

import pytest

from oxenclaw.security.sandbox_context import resolve_sandbox_context


def test_mode_none_passes_through(tmp_path: Path) -> None:
    ctx = resolve_sandbox_context(workspace_dir=tmp_path, mode="none")
    assert not ctx.enabled
    assert ctx.write_allowed
    assert ctx.effective_workspace == tmp_path.resolve()


def test_mode_rw_writes_allowed(tmp_path: Path) -> None:
    ctx = resolve_sandbox_context(workspace_dir=tmp_path, mode="rw")
    assert ctx.enabled
    assert ctx.write_allowed
    assert ctx.effective_workspace == tmp_path.resolve()


def test_mode_ro_writes_denied(tmp_path: Path) -> None:
    ctx = resolve_sandbox_context(workspace_dir=tmp_path, mode="ro")
    assert ctx.enabled
    assert not ctx.write_allowed
    # Still pointed at canonical (no copy made).
    assert ctx.effective_workspace == tmp_path.resolve()


def test_mode_copy_creates_scratch_with_contents(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "alpha.md").write_text("hello")
    (src / "sub").mkdir()
    (src / "sub" / "beta.md").write_text("world")

    ctx = resolve_sandbox_context(workspace_dir=src, mode="copy")
    try:
        assert ctx.enabled
        assert ctx.is_copy
        assert ctx.write_allowed
        assert ctx.effective_workspace != ctx.canonical_workspace
        # Files copied.
        assert (ctx.effective_workspace / "alpha.md").read_text() == "hello"
        assert (ctx.effective_workspace / "sub" / "beta.md").read_text() == "world"
        # Mutations stay isolated.
        (ctx.effective_workspace / "alpha.md").write_text("CHANGED")
        assert (src / "alpha.md").read_text() == "hello"
    finally:
        ctx.cleanup()
        assert not ctx.effective_workspace.exists()


def test_unknown_mode_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        resolve_sandbox_context(workspace_dir=tmp_path, mode="bogus")  # type: ignore[arg-type]
