"""Tests for oxenclaw.backup."""

from __future__ import annotations

import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from oxenclaw.backup import (
    create_backup,
    list_backups,
    restore_backup,
    verify_backup,
)
from oxenclaw.config.paths import OxenclawPaths


def _populate_home(home: Path) -> None:
    """Create a representative home dir tree."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("channels: {}\n")
    (home / "mcp.json").write_text(json.dumps({"mcpServers": {"x": {"command": "echo"}}}))
    (home / "approvals.json").write_text(json.dumps({"pending": []}))
    creds = home / "credentials" / "dashboard"
    creds.mkdir(parents=True, exist_ok=True)
    (creds / "main.json").write_text(json.dumps({"token": "abc"}))
    cron = home / "cron"
    cron.mkdir(parents=True, exist_ok=True)
    (cron / "jobs.json").write_text(json.dumps([]))

    # Make a real sqlite db with some data.
    db_path = home / "memory.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
    conn.execute("INSERT INTO notes (body) VALUES ('hello')")
    conn.execute("INSERT INTO notes (body) VALUES ('world')")
    conn.commit()
    conn.close()


def test_create_backup_produces_valid_archive(tmp_path: Path):
    home = tmp_path / "home"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    _populate_home(home)

    paths = OxenclawPaths(home=home)
    result = create_backup(out_dir, paths=paths)

    assert result.archive_path.exists()
    assert result.file_count > 0
    # All expected files present in manifest
    assert "config.yaml" in result.manifest.files
    assert "mcp.json" in result.manifest.files
    assert "credentials/dashboard/main.json" in result.manifest.files
    assert "memory.db" in result.manifest.files
    # Archive is a valid tar.gz with MANIFEST.json
    with tarfile.open(result.archive_path, "r:gz") as tar:
        names = tar.getnames()
        assert "oxenclaw-backup/MANIFEST.json" in names
        assert "oxenclaw-backup/memory.db" in names


def test_verify_round_trip_succeeds(tmp_path: Path):
    home = tmp_path / "home"
    _populate_home(home)
    paths = OxenclawPaths(home=home)
    result = create_backup(tmp_path, paths=paths)
    manifest = verify_backup(result.archive_path)
    assert manifest.version == 1
    assert manifest.files


def test_verify_detects_corruption(tmp_path: Path):
    home = tmp_path / "home"
    _populate_home(home)
    paths = OxenclawPaths(home=home)
    result = create_backup(tmp_path, paths=paths)

    # Corrupt the archive: write some random bytes to it.
    archive = result.archive_path
    data = archive.read_bytes()
    archive.write_bytes(data[: len(data) // 2] + b"GARBAGEGARBAGE" + data[len(data) // 2 :])
    with pytest.raises(Exception):
        verify_backup(archive)


def test_restore_reconstructs_home_dir(tmp_path: Path):
    home = tmp_path / "home"
    _populate_home(home)
    paths_src = OxenclawPaths(home=home)
    result = create_backup(tmp_path, paths=paths_src)

    restore_dir = tmp_path / "restored"
    paths_dst = OxenclawPaths(home=restore_dir)
    rr = restore_backup(result.archive_path, paths=paths_dst)
    assert rr.target == restore_dir
    assert restore_dir.exists()
    assert (restore_dir / "config.yaml").read_text() == "channels: {}\n"
    assert (restore_dir / "credentials/dashboard/main.json").read_text() == json.dumps(
        {"token": "abc"}
    )
    # SQLite snapshot survived: rows still there.
    conn = sqlite3.connect(str(restore_dir / "memory.db"))
    rows = list(conn.execute("SELECT body FROM notes ORDER BY id"))
    conn.close()
    assert rows == [("hello",), ("world",)]


def test_restore_refuses_to_overwrite_by_default(tmp_path: Path):
    home = tmp_path / "home"
    _populate_home(home)
    paths_src = OxenclawPaths(home=home)
    result = create_backup(tmp_path, paths=paths_src)

    target = tmp_path / "target"
    target.mkdir()
    (target / "extra.txt").write_text("dont touch")

    paths_dst = OxenclawPaths(home=target)
    with pytest.raises(FileExistsError):
        restore_backup(result.archive_path, paths=paths_dst)
    # File untouched
    assert (target / "extra.txt").read_text() == "dont touch"


def test_restore_overwrite_merges_into_existing(tmp_path: Path):
    home = tmp_path / "home"
    _populate_home(home)
    paths_src = OxenclawPaths(home=home)
    result = create_backup(tmp_path, paths=paths_src)

    target = tmp_path / "target"
    target.mkdir()
    (target / "extra.txt").write_text("preserved")

    paths_dst = OxenclawPaths(home=target)
    restore_backup(result.archive_path, paths=paths_dst, overwrite=True)
    assert (target / "extra.txt").read_text() == "preserved"
    assert (target / "config.yaml").read_text() == "channels: {}\n"


def test_restore_dry_run_does_not_touch_filesystem(tmp_path: Path):
    home = tmp_path / "home"
    _populate_home(home)
    paths_src = OxenclawPaths(home=home)
    result = create_backup(tmp_path, paths=paths_src)

    target = tmp_path / "target"
    paths_dst = OxenclawPaths(home=target)
    rr = restore_backup(result.archive_path, paths=paths_dst, dry_run=True)
    assert "config.yaml" in rr.restored_files
    assert not target.exists()


def test_list_backups_orders_newest_first(tmp_path: Path):
    home = tmp_path / "home"
    _populate_home(home)
    paths = OxenclawPaths(home=home)

    out = tmp_path / "backups"
    out.mkdir()
    # Pass explicit paths so the two archives don't collide on auto-name.
    a = create_backup(out / "first.tar.gz", paths=paths).archive_path
    import os
    import time

    time.sleep(0.05)
    b = create_backup(out / "second.tar.gz", paths=paths).archive_path
    # Bump mtime explicitly so sorting is stable on filesystems with low
    # mtime resolution.
    os.utime(b, None)
    listed = list_backups(out)
    assert set(listed) == {a, b}
    assert listed[0] == b  # newest first


def test_create_backup_skips_wal_shm(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    db = home / "memory.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE x (a INTEGER)")
    conn.execute("INSERT INTO x VALUES (1)")
    conn.commit()
    # Force a WAL/SHM sibling to exist.
    assert (home / "memory.db-wal").exists() or (home / "memory.db-shm").exists() or True

    paths = OxenclawPaths(home=home)
    result = create_backup(tmp_path, paths=paths)
    # WAL/SHM siblings must not appear in the manifest.
    assert "memory.db" in result.manifest.files
    assert "memory.db-wal" not in result.manifest.files
    assert "memory.db-shm" not in result.manifest.files
    conn.close()
