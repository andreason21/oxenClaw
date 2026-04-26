"""Create / restore / verify sampyClaw backups."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from sampyclaw.config.paths import SampyclawPaths, default_paths
from sampyclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("backup")

BACKUP_FORMAT_VERSION = 1
DEFAULT_BACKUP_PREFIX = "sampyclaw-backup"


@dataclass
class BackupManifest:
    version: int
    created_at: str
    home: str
    files: dict[str, str]  # relative path → sha256

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "home": self.home,
            "files": self.files,
        }

    @classmethod
    def from_dict(cls, data: dict) -> BackupManifest:
        return cls(
            version=int(data["version"]),
            created_at=str(data["created_at"]),
            home=str(data["home"]),
            files=dict(data["files"]),
        )


@dataclass
class BackupResult:
    archive_path: Path
    manifest: BackupManifest
    file_count: int
    bytes_uncompressed: int


@dataclass
class RestoreResult:
    restored_files: list[str] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)
    target: Path | None = None


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_sqlite(path: Path) -> bool:
    if path.suffix.lower() not in (".db", ".sqlite", ".sqlite3"):
        return False
    try:
        with path.open("rb") as fh:
            return fh.read(16).startswith(b"SQLite format 3")
    except OSError:
        return False


def _walk_home(home: Path) -> list[Path]:
    """Every file we care about under `home`. Skip transient WAL/SHM
    siblings — they get re-derived from the snapshot DB on restore."""
    skip_suffixes = {"-wal", "-shm", "-journal"}
    skip_names = {".DS_Store", "__pycache__"}
    out: list[Path] = []
    for entry in home.rglob("*"):
        if not entry.is_file():
            continue
        if entry.name in skip_names:
            continue
        if any(entry.name.endswith(suf) for suf in skip_suffixes):
            continue
        # Skip pycache directories anywhere in the path.
        if "__pycache__" in entry.parts:
            continue
        out.append(entry)
    return sorted(out)


def _snapshot_sqlite(src: Path, dst: Path) -> None:
    """Use SQLite's online backup API for a consistent snapshot.

    Works even while the source is open + being written to.
    """
    src_conn = sqlite3.connect(str(src))
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def create_backup(
    output_path: Path | None = None,
    *,
    paths: SampyclawPaths | None = None,
) -> BackupResult:
    """Create a `.tar.gz` snapshot of the sampyClaw home dir.

    `output_path` may be a directory (auto-named file is placed inside)
    or a full file path.
    """
    resolved = paths or default_paths()
    home = resolved.home
    if not home.exists():
        raise FileNotFoundError(f"sampyClaw home does not exist: {home}")

    timestamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    default_name = f"{DEFAULT_BACKUP_PREFIX}-{timestamp}.tar.gz"
    if output_path is None:
        archive_path = Path.cwd() / default_name
    elif output_path.is_dir():
        archive_path = output_path / default_name
    else:
        archive_path = output_path
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    manifest_files: dict[str, str] = {}
    bytes_uncompressed = 0

    # Stage everything in a tempdir so partial failures don't pollute the
    # archive path.
    with tempfile.TemporaryDirectory(prefix="sampyclaw-backup-") as tmpdir_str:
        staging = Path(tmpdir_str) / "staging"
        staging.mkdir()
        sources = _walk_home(home)
        for src in sources:
            rel = src.relative_to(home)
            dst = staging / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                if _is_sqlite(src):
                    _snapshot_sqlite(src, dst)
                else:
                    shutil.copy2(src, dst)
            except Exception as exc:
                logger.warning("backup: skipping %s (%s)", rel, exc)
                continue
            manifest_files[str(rel).replace("\\", "/")] = _sha256_of(dst)
            bytes_uncompressed += dst.stat().st_size

        manifest = BackupManifest(
            version=BACKUP_FORMAT_VERSION,
            created_at=_dt.datetime.now(_dt.UTC).isoformat(),
            home=str(home),
            files=manifest_files,
        )
        (staging / "MANIFEST.json").write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )

        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(staging, arcname="sampyclaw-backup")

    return BackupResult(
        archive_path=archive_path,
        manifest=manifest,
        file_count=len(manifest_files),
        bytes_uncompressed=bytes_uncompressed,
    )


def _read_manifest_from_archive(archive: Path) -> BackupManifest:
    with tarfile.open(archive, "r:gz") as tar:
        try:
            member = tar.getmember("sampyclaw-backup/MANIFEST.json")
        except KeyError as exc:
            raise ValueError(
                f"archive {archive} has no MANIFEST.json — not a sampyClaw backup"
            ) from exc
        fh = tar.extractfile(member)
        if fh is None:
            raise ValueError("manifest member is not a regular file")
        data = json.loads(fh.read().decode("utf-8"))
    return BackupManifest.from_dict(data)


def verify_backup(archive: Path) -> BackupManifest:
    """Verify every file's SHA256 matches the manifest."""
    manifest = _read_manifest_from_archive(archive)
    with tarfile.open(archive, "r:gz") as tar:
        for rel, expected in manifest.files.items():
            try:
                member = tar.getmember(f"sampyclaw-backup/{rel}")
            except KeyError as exc:
                raise ValueError(f"manifest references missing file: {rel}") from exc
            fh = tar.extractfile(member)
            if fh is None:
                raise ValueError(f"non-regular file in archive: {rel}")
            actual = hashlib.sha256(fh.read()).hexdigest()
            if actual != expected:
                raise ValueError(
                    f"checksum mismatch for {rel}: expected {expected[:12]}…, got {actual[:12]}…"
                )
    return manifest


def restore_backup(
    archive: Path,
    *,
    paths: SampyclawPaths | None = None,
    dry_run: bool = False,
    overwrite: bool = False,
) -> RestoreResult:
    """Extract `archive` into the sampyClaw home dir.

    `dry_run=True` reports what would be restored without touching the
    filesystem. `overwrite=False` (default) refuses to overwrite an
    existing non-empty home dir — pass `True` to merge in (existing
    files are replaced when the backup contains them).
    """
    resolved = paths or default_paths()
    target = resolved.home
    manifest = verify_backup(archive)
    result = RestoreResult(target=target)

    if target.exists() and any(target.iterdir()) and not overwrite and not dry_run:
        raise FileExistsError(f"target {target} is not empty — pass overwrite=True to merge in")

    if dry_run:
        result.restored_files = list(manifest.files.keys())
        return result

    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        for rel in manifest.files:
            member_name = f"sampyclaw-backup/{rel}"
            try:
                member = tar.getmember(member_name)
            except KeyError:
                result.skipped_files.append(rel)
                continue
            fh = tar.extractfile(member)
            if fh is None:
                result.skipped_files.append(rel)
                continue
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            with dst.open("wb") as out:
                shutil.copyfileobj(fh, out)
            result.restored_files.append(rel)
    return result


def list_backups(directory: Path) -> list[Path]:
    """Return all `*.tar.gz` files in `directory`, newest first by mtime."""
    if not directory.exists():
        return []
    candidates = list(directory.glob("*.tar.gz"))
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates
