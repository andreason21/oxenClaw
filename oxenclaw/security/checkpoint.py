"""Shadow-git checkpoint manager.

Ports hermes-agent's ``tools/checkpoint_manager.py`` concept into oxenclaw:
transparent filesystem snapshots stored in a per-project shadow git repo
under ``~/.oxenclaw/checkpoints/<sha256-of-dir>[:16]/``. The user's
project ``.git/`` is never touched — every git invocation runs against
the shadow ``GIT_DIR`` with ``GIT_WORK_TREE`` pointed at the project.

The shadow repo is fully isolated from the user's global / system git
config so ``commit.gpgsign``, credential helpers, and pinentry prompts
never block a snapshot. ``GIT_CONFIG_GLOBAL=/dev/null`` +
``GIT_CONFIG_SYSTEM=/dev/null`` + ``GIT_CONFIG_NOSYSTEM=1`` is the
non-negotiable isolation pattern; do not weaken it.

Public API:
  * ``CheckpointManager(project_dir, checkpoints_root=...)``
  * ``init()`` — idempotent shadow-repo bootstrap + initial commit
  * ``snapshot(label) -> str`` — returns the new commit hash
  * ``list_snapshots() -> list[(hash, label, ts)]``
  * ``restore(commit_hash)`` — checkout into the work tree
  * ``diff(commit_hash) -> str`` — staged-vs-checkpoint diff text

The run loop never auto-snapshots — the manager is opt-in via
``RuntimeConfig.checkpoint_manager`` and downstream tools call into it.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("security.checkpoint")


# Default git timeout — generous so big repos don't trip on slow disks.
_GIT_TIMEOUT_SECONDS = 30

# 4-64 hex chars cover SHA-1 short, full, and SHA-256 commit hashes.
_COMMIT_HASH_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")

# Shell metachars that must not appear in a commit hash (defence-in-
# depth even after the hex regex; cheap and obvious).
_SHELL_META = set('|&;<>$`"\\\n\r\t ()*?[]{}')

DEFAULT_EXCLUDES = (
    ".git/",
    "node_modules/",
    "dist/",
    "build/",
    ".env",
    ".env.*",
    ".env.local",
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    ".DS_Store",
    "*.log",
    ".cache/",
    ".pytest_cache/",
    ".venv/",
    "venv/",
)


def _default_checkpoints_root() -> Path:
    return Path.home() / ".oxenclaw" / "checkpoints"


def _normalize(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _validate_commit_hash(commit_hash: str) -> str | None:
    """Return error string if invalid, None if OK."""
    if not commit_hash or not commit_hash.strip():
        return "Empty commit hash"
    if commit_hash.startswith("-"):
        return f"Invalid commit hash (must not start with '-'): {commit_hash!r}"
    if any(ch in _SHELL_META for ch in commit_hash):
        return f"Invalid commit hash (shell metachar): {commit_hash!r}"
    if not _COMMIT_HASH_RE.match(commit_hash):
        return f"Invalid commit hash (expected 4-64 hex characters): {commit_hash!r}"
    return None


def _validate_file_path(file_path: str, project_dir: Path) -> str | None:
    """Reject empty / absolute / traversal-escape paths."""
    if not file_path or not file_path.strip():
        return "Empty file path"
    if os.path.isabs(file_path):
        return f"File path must be relative, got absolute path: {file_path!r}"
    abs_workdir = _normalize(project_dir)
    resolved = (abs_workdir / file_path).resolve()
    try:
        resolved.relative_to(abs_workdir)
    except ValueError:
        return f"File path escapes the working directory: {file_path!r}"
    return None


@dataclass(frozen=True)
class Snapshot:
    """One checkpoint entry."""

    commit_hash: str
    label: str
    timestamp: str  # ISO8601


class CheckpointManager:
    """Shadow-git checkpoint manager.

    Each project_dir maps deterministically to one shadow repo under
    ``checkpoints_root / sha256(abs_dir)[:16]``. Construction does not
    create the repo — call ``init()`` (idempotent) before snapshotting.
    """

    def __init__(
        self,
        project_dir: str | Path,
        checkpoints_root: Path | None = None,
    ) -> None:
        self.project_dir: Path = _normalize(project_dir)
        self.checkpoints_root: Path = (
            _normalize(checkpoints_root) if checkpoints_root else _default_checkpoints_root()
        )

    # ─── Path resolution ──────────────────────────────────────────────

    @property
    def shadow_repo_path(self) -> Path:
        return self._shadow_repo_path()

    def _shadow_repo_path(self) -> Path:
        digest = hashlib.sha256(str(self.project_dir).encode("utf-8")).hexdigest()[:16]
        return self.checkpoints_root / digest

    # ─── Subprocess plumbing ──────────────────────────────────────────

    def _git_env(self) -> dict[str, str]:
        """Build subprocess env that fully isolates the shadow repo.

        Critical: GIT_CONFIG_GLOBAL=/dev/null + GIT_CONFIG_SYSTEM=/dev/null
        + GIT_CONFIG_NOSYSTEM=1 — without these the user's global config
        (commit.gpgsign, signing hooks, credential helpers, pinentry
        popups) leaks into background snapshots and either breaks them
        or spawns interactive prompts.
        """
        env = os.environ.copy()
        env["GIT_DIR"] = str(self._shadow_repo_path())
        env["GIT_WORK_TREE"] = str(self.project_dir)
        env.pop("GIT_INDEX_FILE", None)
        env.pop("GIT_NAMESPACE", None)
        env.pop("GIT_ALTERNATE_OBJECT_DIRECTORIES", None)
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        return env

    def _run_git(
        self,
        args: list[str],
        *,
        timeout: int = _GIT_TIMEOUT_SECONDS,
        allowed_returncodes: set[int] | None = None,
    ) -> tuple[bool, str, str]:
        if not self.project_dir.exists():
            return False, "", f"working directory not found: {self.project_dir}"
        if not self.project_dir.is_dir():
            return False, "", f"working directory is not a directory: {self.project_dir}"
        cmd = ["git", *args]
        env = self._git_env()
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                cwd=str(self.project_dir),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, "", f"git timed out after {timeout}s: {' '.join(cmd)}"
        except FileNotFoundError as exc:
            if getattr(exc, "filename", None) == "git":
                return False, "", "git executable not found"
            return False, "", str(exc)
        except Exception as exc:
            return False, "", str(exc)
        ok = res.returncode == 0
        allowed = allowed_returncodes or set()
        stdout = res.stdout.strip()
        stderr = res.stderr.strip()
        if not ok and res.returncode not in allowed:
            logger.debug(
                "git command failed: %s (rc=%d) stderr=%s",
                " ".join(cmd),
                res.returncode,
                stderr,
            )
        return ok, stdout, stderr

    # ─── init() ──────────────────────────────────────────────────────

    def init(self) -> bool:
        """Create the shadow repo and an initial commit if needed.

        Idempotent: returns True on first init or when an init already
        exists; False only when the underlying ``git init`` command
        fails (filesystem error, missing git binary).
        """
        shadow = self._shadow_repo_path()
        if (shadow / "HEAD").exists():
            return True
        shadow.mkdir(parents=True, exist_ok=True)
        ok, _, err = self._run_git(["init"])
        if not ok:
            logger.warning("checkpoint init failed: %s", err)
            return False
        # Per-repo identity + sign-off disabled (belt + suspenders alongside
        # the GIT_CONFIG_* env isolation).
        self._run_git(["config", "user.email", "oxenclaw@local"])
        self._run_git(["config", "user.name", "oxenClaw Checkpoint"])
        self._run_git(["config", "commit.gpgsign", "false"])
        self._run_git(["config", "tag.gpgSign", "false"])
        # Default excludes so transient junk doesn't pollute snapshots.
        info_dir = shadow / "info"
        info_dir.mkdir(exist_ok=True)
        (info_dir / "exclude").write_text("\n".join(DEFAULT_EXCLUDES) + "\n", encoding="utf-8")
        # Initial empty commit so list_snapshots() always has at least
        # one anchor.
        self._run_git(["add", "-A"], timeout=_GIT_TIMEOUT_SECONDS * 2)
        self._run_git(
            [
                "commit",
                "-m",
                "init",
                "--allow-empty",
                "--allow-empty-message",
                "--no-gpg-sign",
            ],
            timeout=_GIT_TIMEOUT_SECONDS * 2,
        )
        return True

    # ─── snapshot() ──────────────────────────────────────────────────

    def snapshot(self, label: str = "auto") -> str:
        """Stage everything in the work tree and commit. Returns the
        commit hash. Raises RuntimeError if the snapshot couldn't land.

        Allows empty commits so a label-only checkpoint is always
        possible (useful for marking decision boundaries).
        """
        if not self.init():
            raise RuntimeError("checkpoint init failed")
        ok, _, err = self._run_git(["add", "-A"], timeout=_GIT_TIMEOUT_SECONDS * 2)
        if not ok:
            raise RuntimeError(f"git add failed: {err}")
        # ``--allow-empty`` so a label-only snapshot always lands.
        ok, _, err = self._run_git(
            [
                "commit",
                "-m",
                label,
                "--allow-empty",
                "--allow-empty-message",
                "--no-gpg-sign",
            ],
            timeout=_GIT_TIMEOUT_SECONDS * 2,
        )
        if not ok:
            raise RuntimeError(f"git commit failed: {err}")
        ok, head, err = self._run_git(["rev-parse", "HEAD"])
        if not ok:
            raise RuntimeError(f"git rev-parse HEAD failed: {err}")
        return head.strip()

    # ─── list_snapshots() ────────────────────────────────────────────

    def list_snapshots(self) -> list[Snapshot]:
        if not (self._shadow_repo_path() / "HEAD").exists():
            return []
        ok, out, _ = self._run_git(["log", "--format=%H|%s|%aI"])
        if not ok or not out:
            return []
        results: list[Snapshot] = []
        for line in out.splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3:
                results.append(
                    Snapshot(
                        commit_hash=parts[0],
                        label=parts[1],
                        timestamp=parts[2],
                    )
                )
        return results

    # ─── restore() ───────────────────────────────────────────────────

    def restore(self, commit_hash: str, file_path: str | None = None) -> None:
        """Restore the work tree to a checkpoint. Raises ValueError on
        invalid input, RuntimeError on git failure."""
        err = _validate_commit_hash(commit_hash)
        if err:
            raise ValueError(err)
        if file_path is not None:
            path_err = _validate_file_path(file_path, self.project_dir)
            if path_err:
                raise ValueError(path_err)
        if not (self._shadow_repo_path() / "HEAD").exists():
            raise RuntimeError("no checkpoints exist for this directory")
        # Verify the commit is reachable.
        ok, _, cat_err = self._run_git(["cat-file", "-t", commit_hash])
        if not ok:
            raise RuntimeError(f"checkpoint {commit_hash!r} not found: {cat_err}")
        target = file_path if file_path else "."
        ok, _, restore_err = self._run_git(
            ["checkout", commit_hash, "--", target],
            timeout=_GIT_TIMEOUT_SECONDS * 2,
        )
        if not ok:
            raise RuntimeError(f"restore failed: {restore_err}")

    # ─── diff() ──────────────────────────────────────────────────────

    def diff(self, commit_hash: str) -> str:
        """Return the diff between a checkpoint and the current work
        tree. Raises ValueError on invalid hash, RuntimeError on git
        failure."""
        err = _validate_commit_hash(commit_hash)
        if err:
            raise ValueError(err)
        if not (self._shadow_repo_path() / "HEAD").exists():
            raise RuntimeError("no checkpoints exist for this directory")
        ok, _, cat_err = self._run_git(["cat-file", "-t", commit_hash])
        if not ok:
            raise RuntimeError(f"checkpoint {commit_hash!r} not found: {cat_err}")
        # Stage current state into the shadow index for the comparison.
        self._run_git(["add", "-A"], timeout=_GIT_TIMEOUT_SECONDS * 2)
        ok, out, derr = self._run_git(["diff", commit_hash, "--cached", "--no-color"])
        # Don't pollute the shadow index after the diff.
        self._run_git(["reset", "HEAD", "--quiet"])
        if not ok:
            raise RuntimeError(f"diff failed: {derr}")
        return out


__all__ = [
    "CheckpointManager",
    "Snapshot",
    "_validate_commit_hash",
    "_validate_file_path",
]
