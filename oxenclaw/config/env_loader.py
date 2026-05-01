"""Auto-load `~/.oxenclaw/env` into `os.environ` on CLI entry.

Why this exists: `oxenclaw setup llamacpp` writes `OXENCLAW_LLAMACPP_BIN`
+ `OXENCLAW_LLAMACPP_GGUF` to a shell-sourced file at
`~/.oxenclaw/env`. Without something pulling those values in, every
subsequent `oxenclaw gateway start` invocation that wasn't run from a
shell that had already sourced the file would resolve `--provider auto`
to `ollama` (because the GGUF env was missing) and silently fall back
to the wrong backend — exactly the bug reported on 2026-04-29 where
the dashboard's `assistant` agent stayed on `ollama / qwen3.5:9b` even
after a successful wizard run.

Behaviour:

- Parses `KEY=VAL` and `export KEY=VAL` lines (the shapes the wizard
  emits). Lines starting with `#` or blank lines are ignored.
- Strips one layer of surrounding `"` or `'` from the value.
- **Shell-set env vars win by default** (`override=False`). Operators
  who explicitly export a variable in their shell or systemd unit
  override the persisted file without surprise.
- Errors are swallowed: a malformed env file should never break CLI
  startup. The function logs at debug level and returns the count of
  keys actually applied.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def env_file_path() -> Path:
    """Resolve `~/.oxenclaw/env`, honouring `$OXENCLAW_HOME` if set."""
    home_override = os.environ.get("OXENCLAW_HOME", "").strip()
    base = Path(os.path.expanduser(home_override)) if home_override else Path.home() / ".oxenclaw"
    return base / "env"


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def parse_env_file(text: str) -> dict[str, str]:
    """Parse a shell-sourced `KEY=VAL` / `export KEY=VAL` env file.

    Returns the resulting mapping. Malformed lines are skipped silently
    so an out-of-band edit can't deadlock the CLI.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # POSIX env var name rule: first char must be a letter or `_`,
        # rest are alnum / `_`. Anything else is a typo.
        if not key or not (key[0].isalpha() or key[0] == "_"):
            continue
        if not key.replace("_", "").isalnum():
            continue
        out[key] = _strip_quotes(value.strip())
    return out


def load_oxenclaw_env_file(*, path: Path | None = None, override: bool = False) -> int:
    """Load `~/.oxenclaw/env` into `os.environ`.

    Args:
        path: Override the file location (mostly for tests).
        override: When True, persisted values win over shell-set vars.
            Default False — explicit shell `export FOO=bar` always wins
            so operators have an escape hatch.

    Returns: number of keys applied. 0 means the file didn't exist or
    contained nothing parseable.
    """
    target = path if path is not None else env_file_path()
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    except OSError as exc:
        logger.debug("env auto-load: read failed (%s)", exc)
        return 0

    try:
        parsed = parse_env_file(text)
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("env auto-load: parse failed (%s)", exc)
        return 0

    applied = 0
    for key, value in parsed.items():
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        applied += 1
    if applied:
        logger.debug("env auto-load: %d key(s) applied from %s", applied, target)
    return applied


__all__ = [
    "env_file_path",
    "load_oxenclaw_env_file",
    "parse_env_file",
]
