"""Sandboxed runner for IsolatedFunctionTool.

Invoked by the parent as a subprocess:

    python3 -m sampyclaw.security.tool_runner <module:attr>

Reads a JSON dict on stdin (input args), imports `<module>:<attr>`
(must be a sync or async callable returning str | dict), invokes it,
and writes a single JSON envelope on stdout::

    {"ok": true,  "result": "..."}
    {"ok": false, "error": "..."}
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import sys
import traceback


def _resolve(handler_path: str):  # type: ignore[no-untyped-def]
    module_name, attr = handler_path.split(":", 1)
    module = importlib.import_module(module_name)
    obj = module
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


async def _invoke(handler, args: dict):  # type: ignore[no-untyped-def, type-arg]
    if inspect.iscoroutinefunction(handler):
        return await handler(args)
    result = handler(args)
    if inspect.isawaitable(result):
        return await result
    return result


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(json.dumps({"ok": False, "error": "usage: tool_runner <module:attr>"}))
        return 64
    handler_path = argv[1]
    try:
        raw = sys.stdin.read()
        args = json.loads(raw) if raw else {}
        handler = _resolve(handler_path)
        result = asyncio.run(_invoke(handler, args))
    except Exception:
        print(json.dumps({"ok": False, "error": traceback.format_exc(limit=4)}))
        return 1
    print(json.dumps({"ok": True, "result": result}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
