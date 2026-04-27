"""Shadow-git checkpoint manager tests.

The shadow repo never touches the user's `.git/`. We verify isolation
explicitly with a deliberately broken `$HOME/.gitconfig` to prove the
GIT_CONFIG_GLOBAL/SYSTEM env isolation works.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from oxenclaw.security.checkpoint import (
    CheckpointManager,
    _validate_commit_hash,
    _validate_file_path,
)


def _git_available() -> bool:
    return shutil.which("git") is not None


pytestmark = pytest.mark.skipif(not _git_available(), reason="git not installed")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "project"
    p.mkdir()
    (p / "hello.txt").write_text("first content\n", encoding="utf-8")
    return p


@pytest.fixture
def checkpoints_root(tmp_path: Path) -> Path:
    return tmp_path / "checkpoints"


def test_init_is_idempotent(project: Path, checkpoints_root: Path) -> None:
    m = CheckpointManager(project, checkpoints_root=checkpoints_root)
    assert m.init() is True
    shadow = m.shadow_repo_path
    assert (shadow / "HEAD").exists()
    # Calling init() a second time stays True and does not blow up.
    assert m.init() is True
    # No `.git` was created in the project dir.
    assert not (project / ".git").exists()


def test_snapshot_returns_commit_hash(project: Path, checkpoints_root: Path) -> None:
    m = CheckpointManager(project, checkpoints_root=checkpoints_root)
    h = m.snapshot("first label")
    assert isinstance(h, str)
    assert len(h) >= 4
    snaps = m.list_snapshots()
    # We expect both the initial commit and our labelled snapshot.
    labels = [s.label for s in snaps]
    assert "first label" in labels


def test_list_snapshots_returns_most_recent_first(
    project: Path, checkpoints_root: Path
) -> None:
    m = CheckpointManager(project, checkpoints_root=checkpoints_root)
    h1 = m.snapshot("first")
    (project / "hello.txt").write_text("second content\n", encoding="utf-8")
    h2 = m.snapshot("second")
    snaps = m.list_snapshots()
    assert snaps[0].commit_hash == h2
    assert snaps[0].label == "second"
    assert any(s.commit_hash == h1 for s in snaps)


def test_restore_brings_back_old_content(
    project: Path, checkpoints_root: Path
) -> None:
    m = CheckpointManager(project, checkpoints_root=checkpoints_root)
    h1 = m.snapshot("v1")
    (project / "hello.txt").write_text("changed!\n", encoding="utf-8")
    m.snapshot("v2")
    assert (project / "hello.txt").read_text(encoding="utf-8") == "changed!\n"
    m.restore(h1)
    assert (project / "hello.txt").read_text(encoding="utf-8") == "first content\n"


def test_restore_rejects_traversal_path(
    project: Path, checkpoints_root: Path
) -> None:
    m = CheckpointManager(project, checkpoints_root=checkpoints_root)
    h = m.snapshot("v1")
    with pytest.raises(ValueError, match="escapes"):
        m.restore(h, file_path="../etc/passwd")


def test_restore_rejects_dash_prefixed_hash(
    project: Path, checkpoints_root: Path
) -> None:
    m = CheckpointManager(project, checkpoints_root=checkpoints_root)
    m.init()
    with pytest.raises(ValueError, match="must not start with"):
        m.restore("--patch")


def test_validate_commit_hash_rejects_shell_meta() -> None:
    assert _validate_commit_hash("abc123$(rm -rf)") is not None
    assert _validate_commit_hash("abc;ls") is not None
    assert _validate_commit_hash("abc def") is not None
    # Valid hex passes.
    assert _validate_commit_hash("abc123") is None
    assert _validate_commit_hash("0123456789abcdef" * 4) is None


def test_validate_file_path_rejects_traversal(tmp_path: Path) -> None:
    proj = tmp_path / "p"
    proj.mkdir()
    assert _validate_file_path("../escape", proj) is not None
    assert _validate_file_path("/abs/path", proj) is not None
    assert _validate_file_path("ok/path.py", proj) is None


def test_diff_includes_changed_file(project: Path, checkpoints_root: Path) -> None:
    m = CheckpointManager(project, checkpoints_root=checkpoints_root)
    h = m.snapshot("v1")
    (project / "hello.txt").write_text("brand new content\n", encoding="utf-8")
    diff_out = m.diff(h)
    assert "brand new content" in diff_out


def test_isolation_from_broken_user_gitconfig(
    project: Path, checkpoints_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A broken global gitconfig (e.g. invalid signing key, pinentry-only
    credential helper) must not interrupt shadow-repo snapshots.
    """
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    # Write a global config that would normally trip an unisolated git:
    # invalid commit.gpgsign + a non-existent signing key.
    (fake_home / ".gitconfig").write_text(
        "[user]\n"
        "    name = no-such-user\n"
        "    email = invalid@example.com\n"
        "    signingkey = NO_SUCH_KEY\n"
        "[commit]\n"
        "    gpgsign = true\n"
        "[gpg]\n"
        "    program = /nonexistent/gpg\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("GIT_CONFIG_SYSTEM", raising=False)
    # Sanity: confirm the global config is "active" for a non-isolated
    # subprocess (so the test would meaningfully fail without isolation).
    res = subprocess.run(
        ["git", "config", "--global", "--get", "commit.gpgsign"],
        env={**os.environ, "HOME": str(fake_home)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.stdout.strip() == "true"
    # Now snapshot — must succeed because CheckpointManager pins
    # GIT_CONFIG_GLOBAL=/dev/null inside _git_env().
    m = CheckpointManager(project, checkpoints_root=checkpoints_root)
    h = m.snapshot("isolated")
    assert isinstance(h, str)
    assert any(s.commit_hash == h for s in m.list_snapshots())
