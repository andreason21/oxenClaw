"""Persistent gateway-token bootstrap.

Mirrors openclaw `ensureGatewayStartupAuth` (`src/gateway/startup-auth.ts`):
when the user starts the gateway without configuring a token (no
`--auth-token` flag, no `OXENCLAW_GATEWAY_TOKEN` env var), generate a
random one and persist it under `~/.oxenclaw/gateway-token`. Subsequent
starts pick the same token up automatically — so the user never sees an
unauthenticated gateway accidentally, and copy-paste of the token into
the dashboard form keeps working across restarts.

Resolution precedence (highest first):

1. `--auth-token <value>` CLI flag (explicit override)
2. `OXENCLAW_GATEWAY_TOKEN` environment variable
3. The persisted token file (`~/.oxenclaw/gateway-token`)
4. Freshly generated token, written to the same file

The persisted file is created with mode 0600. Operators who prefer to
keep their token in a secret manager / env var simply set
`OXENCLAW_GATEWAY_TOKEN` and the file path is never read or written.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from oxenclaw.config.paths import OxenclawPaths, default_paths

TOKEN_BYTES = 24  # 192 bits → 48 hex chars; matches openclaw's randomBytes(24).
TOKEN_FILE_NAME = "gateway-token"

TokenSource = Literal["explicit", "env", "persisted", "generated"]


@dataclass(frozen=True)
class ResolvedToken:
    """Outcome of `resolve_or_generate_token`.

    `path` is set only when the persisted file is the source (or has
    just been created), so the CLI banner can show the user where the
    secret lives.
    """

    token: str
    source: TokenSource
    path: Path | None = None


def token_file_path(paths: OxenclawPaths | None = None) -> Path:
    return (paths or default_paths()).home / TOKEN_FILE_NAME


def load_persisted_token(
    paths: OxenclawPaths | None = None,
) -> tuple[str, Path] | None:
    """Return `(token, path)` if the file exists and is non-empty."""
    p = token_file_path(paths)
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    return text, p


def write_persisted_token(token: str, paths: OxenclawPaths | None = None) -> Path:
    """Persist `token` to `~/.oxenclaw/gateway-token` with mode 0600."""
    resolved = paths or default_paths()
    resolved.ensure_home()
    p = token_file_path(resolved)
    p.write_text(token + "\n", encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        # Filesystems that don't support chmod (Windows host volumes via
        # WSL `/mnt/c`) — best effort, don't fail boot over it.
        pass
    return p


def generate_token() -> str:
    """48-char hex (192 bits of entropy)."""
    return secrets.token_hex(TOKEN_BYTES)


def resolve_or_generate_token(
    *,
    explicit: str | None = None,
    paths: OxenclawPaths | None = None,
    rotate: bool = False,
    env: dict[str, str] | None = None,
) -> ResolvedToken:
    """Resolve the gateway token, generating + persisting one if needed.

    `rotate=True` discards a persisted token and generates a fresh one.
    Useful for the `gateway token --rotate` subcommand.

    `env` is exposed for tests so they can inject an isolated env dict
    without mutating `os.environ`.
    """
    if explicit and explicit.strip():
        return ResolvedToken(token=explicit.strip(), source="explicit")
    env_map = env if env is not None else dict(os.environ)
    env_token = env_map.get("OXENCLAW_GATEWAY_TOKEN")
    if env_token and env_token.strip():
        return ResolvedToken(token=env_token.strip(), source="env")
    if not rotate:
        loaded = load_persisted_token(paths)
        if loaded is not None:
            token, path = loaded
            return ResolvedToken(token=token, source="persisted", path=path)
    new_token = generate_token()
    path = write_persisted_token(new_token, paths)
    return ResolvedToken(token=new_token, source="generated", path=path)


def format_startup_banner(resolved: ResolvedToken, *, host: str, port: int) -> str:
    """Multi-line operator banner showing the token + dashboard URL.

    Suppress the actual secret when the source is `env` or `explicit` —
    the operator already has it. We still print the dashboard URL with
    a placeholder so they know where to point a browser.
    """
    base = f"http://{host}:{port}/"
    bar = "─" * 60
    lines = [bar, "  oxenClaw gateway ready", bar]
    if resolved.source == "generated":
        lines.append(f"  • a fresh gateway token was generated and saved to {resolved.path}")
        lines.append(f"  • token: {resolved.token}")
        lines.append(f"  • open: {base}?token={resolved.token}")
        lines.append("    (the dashboard sets a 12h cookie on first load and strips")
        lines.append("     the token from the address bar)")
    elif resolved.source == "persisted":
        lines.append(f"  • loaded persisted gateway token from {resolved.path}")
        lines.append(f"  • token: {resolved.token}")
        lines.append(f"  • open: {base}?token={resolved.token}")
        lines.append("  • rotate with `oxenclaw gateway token --rotate`")
    elif resolved.source == "env":
        lines.append("  • using OXENCLAW_GATEWAY_TOKEN from the environment")
        lines.append(f"  • open: {base}  (paste your token in the login form)")
    else:  # explicit
        lines.append("  • using --auth-token from the command line")
        lines.append(f"  • open: {base}  (paste your token in the login form)")
    lines.append(bar)
    return "\n".join(lines)


__all__ = [
    "TOKEN_FILE_NAME",
    "ResolvedToken",
    "format_startup_banner",
    "generate_token",
    "load_persisted_token",
    "resolve_or_generate_token",
    "token_file_path",
    "write_persisted_token",
]
