"""Environment variable substitution in config values.

Supports `$NAME` and `${NAME}` and `${NAME:-default}` forms. Unknown vars
without a default raise `MissingEnvVar` so mis-configs fail loudly instead
of silently becoming empty strings.
"""

from __future__ import annotations

import os
import re
from typing import Any

_PATTERN = re.compile(r"\$(?:\{(?P<braced>[^}]+)\}|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))")


class MissingEnvVar(KeyError):
    """Raised when config references an env var that is not set and has no default."""


def substitute(value: Any, env: dict[str, str] | None = None) -> Any:
    """Recursively substitute env vars in a parsed YAML/JSON structure."""
    environ = env if env is not None else dict(os.environ)
    return _walk(value, environ)


def _walk(value: Any, env: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _sub_string(value, env)
    if isinstance(value, list):
        return [_walk(v, env) for v in value]
    if isinstance(value, dict):
        return {k: _walk(v, env) for k, v in value.items()}
    return value


def _sub_string(text: str, env: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("bare")
        default: str | None = None
        if ":-" in name:
            name, default = name.split(":-", 1)
        if name in env:
            return env[name]
        if default is not None:
            return default
        raise MissingEnvVar(f"env var ${{{name}}} not set and no default")

    return _PATTERN.sub(repl, text)
