"""Filesystem tools for CodingAgent: read_file, write_file, list_dir, search_files.

These are intentionally minimal — no approval gating at this layer.
Approval wrapping (via oxenclaw.approvals.tool_wrap.gated_tool) is applied
when CodingAgent builds its curated registry for write-side tools.

TODO (next session): apply_patch tool using the unified-diff format.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from oxenclaw.agents.tools import FunctionTool, Tool


# ── read_file ────────────────────────────────────────────────────────────────


class _ReadFileArgs(BaseModel):
    model_config = {"extra": "forbid"}

    path: str = Field(..., description="Absolute or relative path to the file to read.")
    max_chars: int = Field(
        32_000,
        description="Maximum characters to return. Content is truncated with a notice if exceeded.",
        gt=0,
        le=500_000,
    )


def read_file_tool() -> Tool:
    """Read a file from disk and return its content as text."""

    async def _h(args: _ReadFileArgs) -> str:
        p = Path(args.path)
        if not p.exists():
            return f"read_file error: {p} does not exist"
        if not p.is_file():
            return f"read_file error: {p} is not a regular file"
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"read_file error: {exc}"
        if len(text) > args.max_chars:
            text = text[: args.max_chars] + f"\n[...truncated {len(text) - args.max_chars} chars]"
        return text

    return FunctionTool(
        name="read_file",
        description=(
            "Read the contents of a file and return them as text. "
            "Truncates at `max_chars` (default 32 000) with a notice."
        ),
        input_model=_ReadFileArgs,
        handler=_h,
    )


# ── write_file ───────────────────────────────────────────────────────────────


class _WriteFileArgs(BaseModel):
    model_config = {"extra": "forbid"}

    path: str = Field(..., description="Absolute or relative path to write.")
    content: str = Field(..., description="Full text content to write to the file.")
    create_parents: bool = Field(
        True, description="Create missing parent directories automatically."
    )


def write_file_tool() -> Tool:
    """Write text content to a file, creating it or overwriting it."""

    async def _h(args: _WriteFileArgs) -> str:
        p = Path(args.path)
        try:
            if args.create_parents:
                p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args.content, encoding="utf-8")
        except OSError as exc:
            return f"write_file error: {exc}"
        return f"wrote {len(args.content)} chars to {p}"

    return FunctionTool(
        name="write_file",
        description=(
            "Write text content to a file. Creates or overwrites the file. "
            "Parent directories are created by default. "
            "REQUIRES human approval before execution."
        ),
        input_model=_WriteFileArgs,
        handler=_h,
    )


# ── list_dir ─────────────────────────────────────────────────────────────────


class _ListDirArgs(BaseModel):
    model_config = {"extra": "forbid"}

    path: str = Field(".", description="Directory to list.")
    max_entries: int = Field(
        200,
        description="Maximum number of entries to return.",
        gt=0,
        le=10_000,
    )


def list_dir_tool() -> Tool:
    """List directory contents (one entry per line, dirs end with /)."""

    async def _h(args: _ListDirArgs) -> str:
        p = Path(args.path)
        if not p.exists():
            return f"list_dir error: {p} does not exist"
        if not p.is_dir():
            return f"list_dir error: {p} is not a directory"
        try:
            entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name))
        except OSError as exc:
            return f"list_dir error: {exc}"

        lines: list[str] = []
        for entry in entries[: args.max_entries]:
            suffix = "/" if entry.is_dir() else ""
            lines.append(entry.name + suffix)

        total = len(list(p.iterdir()))
        result = "\n".join(lines)
        if total > args.max_entries:
            result += f"\n[...{total - args.max_entries} more entries not shown]"
        return result

    return FunctionTool(
        name="list_dir",
        description=(
            "List the contents of a directory. Directories end with /. "
            "Returns up to `max_entries` entries sorted (dirs first, then files)."
        ),
        input_model=_ListDirArgs,
        handler=_h,
    )


# ── search_files ─────────────────────────────────────────────────────────────


class _SearchFilesArgs(BaseModel):
    model_config = {"extra": "forbid"}

    root: str = Field(".", description="Directory to search recursively.")
    pattern: str = Field(..., description="Glob pattern for filenames (e.g. '*.py').")
    contains: Optional[str] = Field(
        None,
        description="Optional literal substring; only files containing this string are returned.",
    )
    max_results: int = Field(
        50,
        description="Maximum number of matching paths to return.",
        gt=0,
        le=1_000,
    )


def search_files_tool() -> Tool:
    """Recursively search for files matching a glob pattern, optionally filtering by content."""

    async def _h(args: _SearchFilesArgs) -> str:
        root = Path(args.root)
        if not root.exists():
            return f"search_files error: {root} does not exist"
        if not root.is_dir():
            return f"search_files error: {root} is not a directory"

        matches: list[str] = []
        needle = args.contains

        def _scan() -> list[str]:
            results: list[str] = []
            for dirpath, _dirs, files in os.walk(root):
                for fname in files:
                    if not fnmatch.fnmatch(fname, args.pattern):
                        continue
                    full = os.path.join(dirpath, fname)
                    if needle is not None:
                        try:
                            with open(full, encoding="utf-8", errors="replace") as fh:
                                if needle not in fh.read():
                                    continue
                        except OSError:
                            continue
                    results.append(full)
                    if len(results) >= args.max_results:
                        return results
            return results

        matches = await asyncio.get_event_loop().run_in_executor(None, _scan)
        if not matches:
            return f"search_files: no files matching {args.pattern!r} found under {root}"
        return "\n".join(matches)

    return FunctionTool(
        name="search_files",
        description=(
            "Recursively search for files under `root` matching a glob `pattern` "
            "(e.g. '*.py'). Optionally filter to files whose content contains `contains`. "
            "Returns absolute paths, up to `max_results`."
        ),
        input_model=_SearchFilesArgs,
        handler=_h,
    )


# ── shell_run (thin wrapper around security.shell_tool) ──────────────────────


class _ShellRunArgs(BaseModel):
    model_config = {"extra": "forbid"}

    command: str = Field(
        ...,
        description="Shell command to execute (run via sh -c). Use with caution.",
    )
    timeout_seconds: float = Field(
        30.0,
        description="Hard wall-clock limit in seconds.",
        gt=0,
        le=300,
    )
    cwd: Optional[str] = Field(None, description="Working directory; defaults to process cwd.")


def shell_run_tool() -> Tool:
    """Run an arbitrary shell command via subprocess (NOT sandboxed by default).

    NOTE: This tool REQUIRES human approval before execution when registered
    inside CodingAgent's curated registry.  The approval wrapper is applied in
    coding_agent.py, not here.

    TODO (next session): route through IsolationPolicy / ShellTool for proper
    sandboxing instead of raw asyncio.create_subprocess_shell.
    """

    async def _h(args: _ShellRunArgs) -> str:
        try:
            proc = await asyncio.create_subprocess_shell(
                args.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=args.cwd,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=args.timeout_seconds)
            except TimeoutError:
                proc.kill()
                await proc.communicate()
                return f"shell_run: command timed out after {args.timeout_seconds}s"
        except OSError as exc:
            return f"shell_run error: {exc}"

        stdout = (out or b"").decode("utf-8", errors="replace")
        rc = proc.returncode or 0
        header = f"[exit {rc}]"
        max_chars = 16_000
        if len(stdout) > max_chars:
            stdout = stdout[:max_chars] + f"\n[...truncated {len(stdout) - max_chars} chars]"
        return f"{header}\n{stdout}" if stdout else header

    return FunctionTool(
        name="shell",
        description=(
            "Run an arbitrary shell command (via sh -c) and return stdout+stderr. "
            "REQUIRES human approval before execution. "
            "Exit code is included in the output header."
        ),
        input_model=_ShellRunArgs,
        handler=_h,
    )


def coding_fs_tools() -> list[Tool]:
    """Return the full curated set of filesystem + shell tools for CodingAgent."""
    return [
        read_file_tool(),
        write_file_tool(),
        list_dir_tool(),
        search_files_tool(),
        shell_run_tool(),
    ]


__all__ = [
    "coding_fs_tools",
    "list_dir_tool",
    "read_file_tool",
    "search_files_tool",
    "shell_run_tool",
    "write_file_tool",
]
