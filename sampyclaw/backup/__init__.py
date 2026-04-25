"""Backup + restore for the sampyClaw home directory.

Captures every persistent file under `~/.sampyclaw/`:

- `config.yaml`, `mcp.json`
- `credentials/**/*.json`
- `agents/**/*.json`     (sessions)
- `cron/jobs.json`
- `approvals.json`
- `wiki/**/*.md`
- All `.db` files (memory, audit, pi sessions, …) — sqlite is
  snapshotted via `.backup` API so we capture a consistent point-in-time
  copy even if the gateway is running.

Output is a single `.tar.gz` with a `MANIFEST.json` describing what's
inside and a per-file SHA256 so `restore` can verify integrity.
"""

from sampyclaw.backup.archive import (
    BackupManifest,
    BackupResult,
    RestoreResult,
    create_backup,
    list_backups,
    restore_backup,
    verify_backup,
)

__all__ = [
    "BackupManifest",
    "BackupResult",
    "RestoreResult",
    "create_backup",
    "list_backups",
    "restore_backup",
    "verify_backup",
]
