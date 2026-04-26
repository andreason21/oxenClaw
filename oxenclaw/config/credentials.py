"""Per-channel credential store.

Each (channel, account_id) has a JSON file at
`<home>/credentials/<channel>/<account_id>.json`. Files are written with
mode 0600 to match openclaw's convention.
"""

from __future__ import annotations

import json
import os
import stat
from typing import Any

from oxenclaw.config.paths import OxenclawPaths, default_paths


class CredentialStore:
    def __init__(self, paths: OxenclawPaths | None = None) -> None:
        self._paths = paths or default_paths()

    def read(self, channel: str, account_id: str) -> dict[str, Any] | None:
        path = self._paths.credential_file(channel, account_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"credential file {path} is not a JSON object")
        return data

    def write(self, channel: str, account_id: str, payload: dict[str, Any]) -> None:
        path = self._paths.credential_file(channel, account_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)

    def delete(self, channel: str, account_id: str) -> bool:
        path = self._paths.credential_file(channel, account_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def list_accounts(self, channel: str) -> list[str]:
        channel_dir = self._paths.credentials_dir / channel
        if not channel_dir.exists():
            return []
        return sorted(p.stem for p in channel_dir.glob("*.json"))
