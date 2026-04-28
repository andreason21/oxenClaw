"""Long-running background-process primitive for CodingAgent.

Lets the LLM start a persistent process, send keys/stdin, read stdout, and
stop it.  Mirrors the role of openclaw's ``bash-tools.process.ts``.

Process registry
----------------
Module-level ``_REGISTRY: dict[str, _Process]`` keyed by an 8-hex-char pid.
Each ``_Process`` holds the ``asyncio.subprocess.Process``, a ring-buffer
``bytearray`` capped at 64 KiB, a background reader task, and launch metadata.

Buffer cap
----------
64 KiB (``_BUF_CAP = 65_536``).  When new bytes would overflow the cap the
oldest bytes are dropped from the front so the buffer always contains the
most recent output.

PID format
----------
8 lower-case hex chars, e.g. ``"a3f1b200"``, generated via
``secrets.token_hex(4)``.

Actions
-------
- ``start``        — spawn shell command, register process, return metadata.
- ``send_keys``    — write to stdin, drain stdout briefly, return latest tail.
- ``read_output``  — return latest tail of buffer, non-blocking.
- ``stop``         — SIGTERM → wait 2 s → SIGKILL, drop from registry.
- ``list``         — enumerate active processes with uptime + buffer length.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from oxenclaw.agents.tools import Tool

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BUF_CAP: int = 65_536  # 64 KiB ring-buffer cap per process


# ---------------------------------------------------------------------------
# Internal process record
# ---------------------------------------------------------------------------


class _Process:
    """State for one managed background process."""

    def __init__(
        self,
        pid: str,
        proc: asyncio.subprocess.Process,
        command: str,
        cwd: str | None,
        started_at: str,
    ) -> None:
        self.pid = pid
        self.proc = proc
        self.command = command
        self.cwd = cwd
        self.started_at = started_at  # ISO-8601 UTC string
        self._buf: bytearray = bytearray()
        self._reader_task: asyncio.Task[None] | None = None

    # -- buffer helpers -------------------------------------------------------

    def _append(self, data: bytes) -> None:
        """Append *data* to the ring buffer, dropping from front on overflow."""
        needed = len(self._buf) + len(data)
        if needed > _BUF_CAP:
            drop = needed - _BUF_CAP
            del self._buf[:drop]
        self._buf.extend(data)

    def tail(self, chars: int) -> str:
        """Return the last *chars* characters of the buffer as UTF-8 text."""
        raw = bytes(self._buf)
        if len(raw) > chars:
            raw = raw[-chars:]
        return raw.decode("utf-8", errors="replace")

    def buf_len(self) -> int:
        return len(self._buf)

    # -- background reader task -----------------------------------------------

    def start_reader(self) -> None:
        """Spawn the asyncio task that drains stdout into the ring buffer."""

        async def _read_loop() -> None:
            assert self.proc.stdout is not None
            try:
                while True:
                    chunk = await self.proc.stdout.read(4096)
                    if not chunk:
                        break
                    self._append(chunk)
            except Exception:
                pass

        self._reader_task = asyncio.ensure_future(_read_loop())

    async def stop_reader(self) -> None:
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass


# ---------------------------------------------------------------------------
# Module-level process registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, _Process] = {}


# ---------------------------------------------------------------------------
# Args model
# ---------------------------------------------------------------------------


class _Args(BaseModel):
    model_config = {"extra": "forbid"}

    action: Literal["start", "send_keys", "read_output", "stop", "list"]
    pid: str | None = Field(
        None, description="Process id; required for send_keys / read_output / stop."
    )
    command: str | None = Field(None, description="Shell command to run; required for start.")
    cwd: str | None = Field(None, description="Working directory for start (optional).")
    env: dict[str, str] | None = Field(
        None, description="Extra environment variables for start (optional)."
    )
    keys: str | None = Field(
        None, description="Literal text to write to stdin; required for send_keys."
    )
    timeout_s: float = Field(
        1.0, description="Max seconds to drain stdout (send_keys / read_output).", gt=0
    )
    tail_chars: int = Field(
        4_000, description="Maximum characters to return from the output buffer.", gt=0
    )


# ---------------------------------------------------------------------------
# Action implementations
# ---------------------------------------------------------------------------


async def _action_start(args: _Args) -> dict[str, Any]:
    if not args.command:
        return {"error": "action=start requires 'command'"}

    pid = secrets.token_hex(4)
    started_at = datetime.now(tz=UTC).isoformat()

    merged_env: dict[str, str] | None = None
    if args.env:
        merged_env = {**os.environ, **args.env}

    proc = await asyncio.create_subprocess_shell(
        args.command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.PIPE,
        cwd=args.cwd,
        env=merged_env,
    )

    record = _Process(
        pid=pid,
        proc=proc,
        command=args.command,
        cwd=args.cwd,
        started_at=started_at,
    )
    record.start_reader()
    _REGISTRY[pid] = record

    return {"pid": pid, "command": args.command, "started_at": started_at}


async def _action_send_keys(args: _Args) -> dict[str, Any]:
    if not args.pid:
        return {"error": "action=send_keys requires 'pid'"}
    if args.keys is None:
        return {"error": "action=send_keys requires 'keys'"}

    record = _REGISTRY.get(args.pid)
    if record is None:
        return {"error": f"no active process with pid={args.pid!r}"}
    if record.proc.stdin is None:
        return {"error": "process stdin is not available"}

    payload = args.keys
    if not payload.endswith("\n"):
        payload += "\n"

    try:
        record.proc.stdin.write(payload.encode("utf-8"))
        await record.proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        return {"error": "process stdin is closed (process may have exited)"}

    # Drain stdout for up to timeout_s
    deadline = asyncio.get_event_loop().time() + args.timeout_s
    while asyncio.get_event_loop().time() < deadline:
        remaining = deadline - asyncio.get_event_loop().time()
        await asyncio.sleep(min(0.05, remaining))

    return {"pid": args.pid, "output": record.tail(args.tail_chars)}


async def _action_read_output(args: _Args) -> dict[str, Any]:
    if not args.pid:
        return {"error": "action=read_output requires 'pid'"}

    record = _REGISTRY.get(args.pid)
    if record is None:
        return {"error": f"no active process with pid={args.pid!r}"}

    # Give the reader task a brief moment to flush buffered data (non-blocking)
    await asyncio.sleep(min(0.05, args.timeout_s))

    return {"pid": args.pid, "output": record.tail(args.tail_chars)}


async def _action_stop(args: _Args) -> dict[str, Any]:
    if not args.pid:
        return {"error": "action=stop requires 'pid'"}

    record = _REGISTRY.get(args.pid)
    if record is None:
        return {"error": f"no active process with pid={args.pid!r}"}

    # Capture tail before teardown
    final_output = record.tail(args.tail_chars)

    # SIGTERM first
    try:
        record.proc.terminate()
    except ProcessLookupError:
        pass  # already dead

    # Wait up to 2 s, then SIGKILL
    try:
        await asyncio.wait_for(record.proc.wait(), timeout=2.0)
    except TimeoutError:
        try:
            record.proc.kill()
        except ProcessLookupError:
            pass
        await record.proc.wait()

    await record.stop_reader()
    exit_code = record.proc.returncode
    del _REGISTRY[args.pid]

    return {"pid": args.pid, "exit_code": exit_code, "final_output": final_output}


def _action_list() -> dict[str, Any]:
    now = datetime.now(tz=UTC)
    processes: list[dict[str, Any]] = []
    for pid, record in _REGISTRY.items():
        try:
            started = datetime.fromisoformat(record.started_at)
            uptime_s = (now - started).total_seconds()
        except ValueError:
            uptime_s = -1.0
        processes.append(
            {
                "pid": pid,
                "command": record.command,
                "cwd": record.cwd,
                "started_at": record.started_at,
                "uptime_s": round(uptime_s, 1),
                "buf_bytes": record.buf_len(),
            }
        )
    return {"processes": processes, "count": len(processes)}


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def process_tool() -> Tool:
    """Return a FunctionTool named ``"process"`` for managing background processes.

    Actions: start, send_keys, read_output, stop, list.
    """
    # Late import to avoid circular dependency when process_tool is imported
    # before oxenclaw.agents package is fully initialized.
    from oxenclaw.agents.tools import FunctionTool

    async def _h(args: _Args) -> str:
        if args.action == "start":
            result = await _action_start(args)
        elif args.action == "send_keys":
            result = await _action_send_keys(args)
        elif args.action == "read_output":
            result = await _action_read_output(args)
        elif args.action == "stop":
            result = await _action_stop(args)
        elif args.action == "list":
            result = _action_list()
        else:
            result = {"error": f"unknown action: {args.action!r}"}

        return json.dumps(result, ensure_ascii=False)

    return FunctionTool(
        name="process",
        description=(
            "Manage long-running background processes (dev servers, REPLs, watchers, etc.).\n"
            "Actions:\n"
            "- start: spawn a shell command in the background; returns {pid, command, started_at}.\n"
            "- send_keys: write literal text to the process stdin and return latest stdout tail.\n"
            "- read_output: return the latest stdout tail without blocking (up to tail_chars).\n"
            "- stop: terminate the process (SIGTERM then SIGKILL) and return exit_code + final output.\n"
            "- list: enumerate all active processes with uptime and buffer size.\n"
            "Use 'process' for dev servers / REPLs / file watchers that must stay alive across turns. "
            "REQUIRES human approval before execution (same risk class as shell)."
        ),
        input_model=_Args,
        handler=_h,
    )


__all__ = ["process_tool"]
