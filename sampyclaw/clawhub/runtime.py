"""Skill runtime: env-overrides + ephemeral workspace.

Mirrors openclaw `agents/skills/env-overrides.runtime.ts` + `workspace.ts`.

A skill SKILL.md may declare:

```yaml
metadata:
  openclaw:
    env_overrides:
      MY_TOKEN: "$VAULT_TOKEN"   # `$NAME` expands from the host env
      LOG_LEVEL: "debug"          # literal value
    workspace:
      kind: "ephemeral"          # default — fresh tmpdir per invocation
      retain_on_error: true       # keep the dir if the run failed (debug)
```

`prepare_skill_runtime(skill, paths)` returns a `SkillRuntime` context
manager that:

- Resolves `env_overrides`, expanding `$VARS` against the host env.
- Creates an ephemeral workspace dir under `~/.sampyclaw/skill-workspaces/`.
- Cleans the workspace up on exit (or retains it on failure when
  `retain_on_error=True`).

The `SkillRuntime` is consumed by anything that runs a skill's commands
— shell tools, the upcoming coding-agent skill, etc.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sampyclaw.clawhub.loader import InstalledSkill
from sampyclaw.config.paths import SampyclawPaths, default_paths
from sampyclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("clawhub.runtime")


_ENV_VAR_RE = "$"


@dataclass(frozen=True)
class WorkspaceConfig:
    kind: str = "ephemeral"  # "ephemeral" | "persistent"
    retain_on_error: bool = False


@dataclass
class SkillRuntime:
    """One activation of a skill — ephemeral env + workspace."""

    skill: InstalledSkill
    workspace_dir: Path
    env: dict[str, str]
    config: WorkspaceConfig
    failed: bool = False

    def mark_failed(self) -> None:
        self.failed = True

    def cleanup(self) -> None:
        """Remove the workspace unless we should retain on error."""
        if self.config.kind != "ephemeral":
            return
        if self.failed and self.config.retain_on_error:
            logger.info(
                "retaining workspace %s for skill %s (failed run)",
                self.workspace_dir,
                self.skill.slug,
            )
            return
        try:
            shutil.rmtree(self.workspace_dir, ignore_errors=True)
        except OSError:
            logger.warning("failed to clean workspace %s", self.workspace_dir)


def _expand_env_value(value: Any, host_env: dict[str, str]) -> str:
    """Resolve `$VAR` references against `host_env`, leaving unknown
    references as empty strings (mirrors POSIX shell expansion of unset
    vars). Non-string values are coerced via str()."""
    if not isinstance(value, str):
        return str(value)
    if "$" not in value:
        return value
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch != "$":
            out.append(ch)
            i += 1
            continue
        # Bare `$`. Read until non-identifier char.
        j = i + 1
        # Allow ${VAR} braced form.
        if j < len(value) and value[j] == "{":
            end = value.find("}", j + 1)
            if end == -1:
                out.append(ch)
                i += 1
                continue
            name = value[j + 1 : end]
            out.append(host_env.get(name, ""))
            i = end + 1
            continue
        while j < len(value) and (value[j].isalnum() or value[j] == "_"):
            j += 1
        name = value[i + 1 : j]
        if not name:
            out.append("$")
            i += 1
            continue
        out.append(host_env.get(name, ""))
        i = j
    return "".join(out)


def resolve_env_overrides(
    raw: dict[str, Any] | None,
    *,
    host_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Expand `$VAR` references and coerce values to strings."""
    if not raw:
        return {}
    env_src = host_env if host_env is not None else dict(os.environ)
    return {key: _expand_env_value(val, env_src) for key, val in raw.items()}


def _openclaw_extras(skill: InstalledSkill) -> dict[str, Any]:
    """Return openclaw-block fields beyond the typed ones (env_overrides,
    workspace, etc.). Accepts both root-level `openclaw:` (which the
    manifest types) and nested `metadata.openclaw:` (forward-compat with
    raw SKILL.md layouts)."""
    extras: dict[str, Any] = {}
    oc = skill.manifest.openclaw
    if oc is not None and oc.model_extra:
        extras.update(oc.model_extra)
    raw_extras = skill.manifest.model_extra or {}
    raw_meta = raw_extras.get("metadata")
    if isinstance(raw_meta, dict):
        nested = raw_meta.get("openclaw")
        if isinstance(nested, dict):
            for k, v in nested.items():
                extras.setdefault(k, v)
    return extras


def _read_workspace_config(skill: InstalledSkill) -> WorkspaceConfig:
    extras = _openclaw_extras(skill)
    ws = extras.get("workspace")
    if not isinstance(ws, dict):
        return WorkspaceConfig()
    kind = ws.get("kind", "ephemeral")
    retain = bool(ws.get("retain_on_error", False))
    if kind not in ("ephemeral", "persistent"):
        kind = "ephemeral"
    return WorkspaceConfig(kind=kind, retain_on_error=retain)


def _read_env_overrides(skill: InstalledSkill) -> dict[str, Any]:
    extras = _openclaw_extras(skill)
    raw = extras.get("env_overrides")
    return raw if isinstance(raw, dict) else {}


def _workspace_root(paths: SampyclawPaths) -> Path:
    p = paths.home / "skill-workspaces"
    p.mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def prepare_skill_runtime(
    skill: InstalledSkill,
    *,
    paths: SampyclawPaths | None = None,
    extra_env: dict[str, str] | None = None,
):  # type: ignore[no-untyped-def]
    """Yield a `SkillRuntime` with workspace + resolved env.

    Use as a context manager:
        with prepare_skill_runtime(skill) as rt:
            run_thing(cwd=rt.workspace_dir, env=rt.env)
            if oops:
                rt.mark_failed()
    """
    paths = paths or default_paths()
    config = _read_workspace_config(skill)
    raw_overrides = _read_env_overrides(skill)
    env_overrides = resolve_env_overrides(raw_overrides)

    # Build the runtime env: start with a sanitised baseline, layer in
    # skill overrides + caller-supplied extras.
    base_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(paths.home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    env = {**base_env, **env_overrides}
    if extra_env:
        env.update(extra_env)

    if config.kind == "ephemeral":
        ws = Path(tempfile.mkdtemp(prefix=f"sampyclaw-{skill.slug}-", dir=_workspace_root(paths)))
    else:
        ws = _workspace_root(paths) / skill.slug
        ws.mkdir(parents=True, exist_ok=True)

    rt = SkillRuntime(skill=skill, workspace_dir=ws, env=env, config=config)
    try:
        yield rt
    except BaseException:
        rt.mark_failed()
        raise
    finally:
        rt.cleanup()


__all__ = [
    "SkillRuntime",
    "WorkspaceConfig",
    "prepare_skill_runtime",
    "resolve_env_overrides",
]
