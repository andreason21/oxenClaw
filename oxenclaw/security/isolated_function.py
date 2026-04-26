"""IsolatedFunctionTool — wrap a Python callable Tool to run in a fresh
sandboxed Python subprocess.

A Python tool's handler is a callable defined in our codebase. To run it
under isolation we:

1. Specify the callable by *import path* (`module:attr`).
2. Spawn a fresh `python3 -m oxenclaw.security.tool_runner module:attr`
   subprocess via the isolation backend.
3. Pipe the validated input dict in as JSON on stdin.
4. The runner imports the callable, executes it, prints `{"ok": true, "result": ...}`
   on stdout (or `{"ok": false, "error": ...}`).

The isolation backend (subprocess + bwrap + container) handles the
sandboxing — this just gives Python tools the *same* protection that
ShellTool already gets.

Limitations:
- The handler module must be importable from the subprocess sys.path.
- The tool's input is JSON-serialised, so input/output must be
  JSON-friendly (pydantic models on either side handle this fine).
- Module-level side effects (e.g. opening DB connections) re-run per
  invocation. That's fine for stateless tools; stateful tools should
  stay as in-process FunctionTool.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from oxenclaw.security.isolation.policy import IsolationPolicy
from oxenclaw.security.isolation.registry import resolve_backend
from oxenclaw.security.shell_tool import ShellToolError


def _curated_pythonpath(handler_module: str) -> str:
    """Build a minimal PYTHONPATH covering only what the runner needs.

    The sandbox must not see the host's full site-packages. We include:
    - the directory containing the top-level package of `handler_module`
    - the directory containing the `oxenclaw` package (so the tool_runner
      module can import).
    Modules that exist only in the host venv (requests, etc.) become
    unavailable inside the sandbox, which is the point.
    """
    paths: list[str] = []

    def _root_for(module_name: str) -> str | None:
        try:
            mod = importlib.import_module(module_name)
        except ImportError:
            return None
        spec = getattr(mod, "__spec__", None)
        if spec is None:
            return None
        origin = spec.origin
        if origin is None:
            # Namespace package — use the first search location.
            search_locations = getattr(spec, "submodule_search_locations", None)
            if search_locations:
                return str(Path(next(iter(search_locations))).parent)
            return None
        # `module/__init__.py` → parent.parent gives the dir on sys.path.
        return str(Path(origin).resolve().parent.parent)

    top_level = handler_module.split(".", 1)[0]
    for name in (top_level, "oxenclaw"):
        root = _root_for(name)
        if root and root not in paths:
            paths.append(root)
    return ":".join(paths)


class IsolatedFunctionTool:
    """Tool whose handler is invoked inside an isolated Python subprocess."""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        input_model: type[BaseModel],
        handler_path: str,  # "module.path:callable_name"
        policy: IsolationPolicy | None = None,
    ) -> None:
        if ":" not in handler_path:
            raise ValueError(f"handler_path must be 'module:attr', got {handler_path!r}")
        self.name = name
        self.description = description
        self._input_model = input_model
        self._handler_path = handler_path
        # Build a curated PYTHONPATH covering only the handler's package and
        # oxenclaw — never propagate the host's full site-packages.
        handler_module = handler_path.split(":", 1)[0]
        curated = _curated_pythonpath(handler_module)
        base = policy or IsolationPolicy()
        # Strip any caller-supplied PYTHONPATH passthrough — it would re-leak
        # the host env and defeat the curation we just did.
        passthrough = tuple(k for k in base.env_passthrough if k != "PYTHONPATH")
        inject = tuple((k, v) for (k, v) in base.env_inject if k != "PYTHONPATH")
        if curated:
            inject = (*inject, ("PYTHONPATH", curated))
        self._policy = IsolationPolicy(
            **{
                **base.__dict__,
                "env_passthrough": passthrough,
                "env_inject": inject,
            }
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return self._input_model.model_json_schema()

    @property
    def policy(self) -> IsolationPolicy:
        return self._policy

    async def execute(self, args: dict[str, Any]) -> str:
        parsed = self._input_model.model_validate(args)
        payload = json.dumps(parsed.model_dump()).encode("utf-8")
        # Use the SAME interpreter the gateway is running under so venv +
        # site-packages match — `python3` from PATH may be a different env.
        argv = [
            sys.executable,
            "-m",
            "oxenclaw.security.tool_runner",
            self._handler_path,
        ]
        backend = await resolve_backend(self._policy)
        result = await backend.run(argv, policy=self._policy, stdin=payload)

        if result.timed_out:
            raise ShellToolError(
                f"isolated tool {self.name!r} timed out after {self._policy.timeout_seconds}s",
                result=result,
            )

        # The runner always emits a JSON envelope on stdout — even when
        # the handler raises (exit_code becomes 1 with structured error).
        # Try to parse first; fall back to generic on garbled output.
        envelope: dict[str, Any] | None = None
        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError:
            envelope = None

        if envelope is not None and not envelope.get("ok", False):
            raise ShellToolError(
                f"isolated tool {self.name!r}: {envelope.get('error', 'unknown error')}",
                result=result,
            )
        if envelope is None or result.exit_code != 0 or result.error is not None:
            raise ShellToolError(
                f"isolated tool {self.name!r} failed (exit={result.exit_code}): "
                + (result.stderr.strip() or result.error or "no output")[:300],
                result=result,
            )

        out = envelope.get("result")
        return out if isinstance(out, str) else json.dumps(out)


__all__ = ["IsolatedFunctionTool"]
