"""Filesystem tools for CodingAgent: read_file, write_file, list_dir, edit,
read_pdf, grep, glob, search_files (deprecated alias).

These are intentionally minimal — no approval gating at this layer.
Approval wrapping (via oxenclaw.approvals.tool_wrap.gated_tool) is applied
when CodingAgent builds its curated registry for write-side tools.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.tools_pkg.file_state import get_registry as _file_state_registry
from oxenclaw.tools_pkg.fuzzy_match import FuzzyMatchError, fuzzy_find_and_replace


def _current_task_id() -> str:
    """Best-effort task identity for the file-state registry.

    Falls back to ``"main"`` when no AgentContext is propagated through
    the tool-call stack. The registry tolerates this — single-agent
    callers will always look like the same writer.
    """
    return "main"


def _looks_binary(text: str) -> bool:
    """Heuristic: text decoded cleanly as UTF-8 but is mostly control
    characters (e.g. 0x00–0x1F minus tab/newline/cr/ff) — that's a
    binary blob the strict-UTF-8 check let through."""
    if not text:
        return False
    sample = text[:1024]
    allow = {"\t", "\n", "\r", "\f"}
    ctrl = sum(1 for ch in sample if ord(ch) < 0x20 and ch not in allow)
    return ctrl / len(sample) > 0.05


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
    start_line: int = Field(
        1,
        description="First line to return (1-indexed, inclusive).",
        ge=1,
    )
    end_line: int | None = Field(
        None,
        description="Last line to return (1-indexed, inclusive). None means read to EOF.",
        ge=1,
    )
    with_line_numbers: bool = Field(
        True,
        description="Prefix each line with its 1-indexed line number (4-digit right-aligned, then │).",
    )


def read_file_tool() -> Tool:
    """Read a file from disk and return its content as text.

    Supports line ranges (start_line / end_line) and line-number prefixes.
    Binary files return a sentinel string instead of crashing.
    """

    async def _h(args: _ReadFileArgs) -> str:
        p = Path(args.path)
        if not p.exists():
            return f"read_file error: {p} does not exist"
        if not p.is_file():
            return f"read_file error: {p} is not a regular file"

        # Binary detection: try strict UTF-8 first. UTF-8 alone doesn't
        # catch all binary inputs — bytes(range(16)) decodes cleanly
        # because 0x00–0x7F are all valid single-byte UTF-8. Treat
        # anything that decodes but contains a NUL byte (or has >5%
        # control chars outside \t\n\r\f) as binary as well.
        raw_bytes = p.read_bytes()
        size = len(raw_bytes)
        sniff = raw_bytes[:16].hex()
        try:
            full_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return f"(binary file: {size} bytes, sniff={sniff})"
        if "\x00" in full_text or _looks_binary(full_text):
            return f"(binary file: {size} bytes, sniff={sniff})"

        lines = full_text.splitlines(keepends=True)
        total_lines = len(lines)

        # Apply line range (1-indexed).
        start = max(1, args.start_line)
        end = args.end_line if args.end_line is not None else total_lines
        end = min(end, total_lines)

        selected = lines[start - 1 : end]

        if args.with_line_numbers:
            numbered: list[str] = []
            for i, line in enumerate(selected, start=start):
                # 4-digit right-aligned number, then │, then the line content.
                numbered.append(f"{i:4d}│ {line}")
            text = "".join(numbered)
        else:
            text = "".join(selected)

        if len(text) > args.max_chars:
            text = text[: args.max_chars] + f"\n[...truncated {len(text) - args.max_chars} chars]"

        # Cross-agent file-state coordination: record this read so a
        # later edit/write can warn if a sibling agent (or external
        # editor) modified the file in between.
        try:
            partial = (start > 1) or (end < total_lines)
            _file_state_registry().register_read(_current_task_id(), p, partial=partial)
        except Exception:
            pass
        return text

    return FunctionTool(
        name="read_file",
        description=(
            "Read the contents of a file and return them as text. "
            "Supports start_line/end_line for targeted reads. "
            "Line numbers are prefixed as '   1│ ' by default. "
            "Binary files return a hex-sniff sentinel. "
            "Truncates at `max_chars` (default 32 000) with a notice."
        ),
        input_model=_ReadFileArgs,
        handler=_h,
    )


# ── read_pdf ─────────────────────────────────────────────────────────────────


class _ReadPdfArgs(BaseModel):
    model_config = {"extra": "forbid"}

    path: str = Field(..., description="Absolute or relative path to the PDF file.")
    max_pages: int = Field(
        30,
        description="Maximum number of pages to extract.",
        gt=0,
        le=500,
    )


def read_pdf_tool() -> Tool:
    """Extract text from a PDF file using pypdf (if installed).

    Pages are separated by '--- page N ---' markers.
    Returns an error message if pypdf is not installed.
    """

    async def _h(args: _ReadPdfArgs) -> str:
        try:
            import pypdf  # type: ignore[import-untyped]
        except ImportError:
            return "read_pdf error: pypdf is not installed. Install it with: pip install pypdf"

        p = Path(args.path)
        if not p.exists():
            return f"read_pdf error: {p} does not exist"
        if not p.is_file():
            return f"read_pdf error: {p} is not a regular file"

        try:
            reader = pypdf.PdfReader(str(p))
        except Exception as exc:
            return f"read_pdf error: could not open PDF: {exc}"

        pages_to_read = min(len(reader.pages), args.max_pages)
        parts: list[str] = []
        for i in range(pages_to_read):
            try:
                page_text = reader.pages[i].extract_text() or ""
            except Exception:
                page_text = "(page extraction failed)"
            parts.append(f"--- page {i + 1} ---\n{page_text}")

        return "\n\n".join(parts)

    return FunctionTool(
        name="read_pdf",
        description=(
            "Extract text from a PDF file using pypdf. "
            "Pages are separated by '--- page N ---' markers. "
            "Returns an error if pypdf is not installed."
        ),
        input_model=_ReadPdfArgs,
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
        # Cross-agent staleness check (informative — does NOT block).
        warning_prefix = ""
        try:
            stale = _file_state_registry().check_stale(_current_task_id(), p)
            if stale is not None and stale.kind == "sibling_wrote":
                import time as _t

                ts = (
                    _t.strftime("%H:%M:%S", _t.localtime(stale.last_writer_at))
                    if stale.last_writer_at is not None
                    else "?"
                )
                warning_prefix = (
                    f"[WARN: another agent wrote to this file at {ts}; "
                    "you may be overwriting their changes. If you're "
                    "certain, re-read first.]\n"
                )
        except Exception:
            pass
        try:
            if args.create_parents:
                p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args.content, encoding="utf-8")
        except OSError as exc:
            return f"write_file error: {exc}"
        try:
            _file_state_registry().register_write(_current_task_id(), p)
        except Exception:
            pass
        return f"{warning_prefix}wrote {len(args.content)} chars to {p}"

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


# ── edit ─────────────────────────────────────────────────────────────────────


class _EditArgs(BaseModel):
    model_config = {"extra": "forbid"}

    path: str = Field(..., description="Absolute or relative path to the file to edit.")
    old_str: str = Field(..., description="Exact string to find and replace.")
    new_str: str = Field(..., description="Replacement string.")
    count: int = Field(
        1,
        description=(
            "Expected number of exact occurrences of old_str in the file. "
            "The edit is rejected if the actual count differs."
        ),
        ge=1,
    )


def edit_tool() -> Tool:
    """Targeted string-replace editor — token-efficient, no full rewrites needed.

    Reads the file, counts exact occurrences of old_str, and replaces them
    atomically (tmp file + os.replace). Rejects the edit if the occurrence count
    does not match the expected `count` argument.
    REQUIRES human approval before execution.
    """

    async def _h(args: _EditArgs) -> str:
        p = Path(args.path)
        if not p.exists():
            return f"edit error: {p} does not exist"
        if not p.is_file():
            return f"edit error: {p} is not a regular file"

        # Refuse degenerate inputs.
        if not args.old_str:
            return "edit error: old_str must not be empty"
        if args.old_str == args.new_str:
            return "edit error: old_str and new_str are identical — no-op refused"

        # Read with strict UTF-8 (binary files rejected).
        try:
            text = p.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            return f"edit error: {p} is not a UTF-8 text file"
        except OSError as exc:
            return f"edit error: {exc}"

        # Cross-agent staleness check (informative — does NOT block).
        warning_prefix = ""
        try:
            stale = _file_state_registry().check_stale(_current_task_id(), p)
            if stale is not None and stale.kind == "sibling_wrote":
                import time as _t

                ts = (
                    _t.strftime("%H:%M:%S", _t.localtime(stale.last_writer_at))
                    if stale.last_writer_at is not None
                    else "?"
                )
                warning_prefix = (
                    f"[WARN: another agent wrote to this file at {ts}; "
                    "you may be overwriting their changes. If you're "
                    "certain, re-read first.]\n"
                )
        except Exception:
            pass

        # Multi-strategy fuzzy patcher: tolerates whitespace / indentation /
        # smart-quote drift but bails out on JSON-escape corruption.
        try:
            new_text, _strategy = fuzzy_find_and_replace(
                text,
                args.old_str,
                args.new_str,
                expected_count=args.count,
            )
        except FuzzyMatchError as exc:
            return f"edit error: {exc}"

        # Atomic write: write to a temp file beside the target, then os.replace.
        try:
            dir_ = p.parent
            fd, tmp_path = tempfile.mkstemp(dir=str(dir_))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(new_text)
                os.replace(tmp_path, str(p))
            except Exception:
                # Best-effort cleanup; don't shadow the original error.
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as exc:
            return f"edit error: {exc}"

        try:
            _file_state_registry().register_write(_current_task_id(), p)
        except Exception:
            pass
        return f"{warning_prefix}edited {p}: {args.count} replacement(s)"

    return FunctionTool(
        name="edit",
        description=(
            "Targeted string-replace file editor (token-efficient). "
            "Reads the file, verifies the expected occurrence count of `old_str`, "
            "then replaces and atomically writes. "
            "Rejects if count mismatches, old_str is empty, or old_str == new_str. "
            "REQUIRES human approval before execution."
        ),
        input_model=_EditArgs,
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


# ── grep ──────────────────────────────────────────────────────────────────────


class _GrepArgs(BaseModel):
    model_config = {"extra": "forbid"}

    pattern: str = Field(..., description="Python regex pattern to search for.")
    path: str = Field(".", description="Directory (or file) to search under.")
    glob: str | None = Field(
        None,
        description="Optional glob pattern to restrict which files are searched (e.g. '*.py').",
    )
    max_matches: int = Field(
        100,
        description="Maximum number of matching lines to return.",
        gt=0,
        le=10_000,
    )


def grep_tool() -> Tool:
    """Regex-based content search across files.

    Returns 'path:line_number:matched_line' entries.
    Uses re.compile(pattern, re.MULTILINE).
    """

    async def _h(args: _GrepArgs) -> str:
        try:
            rx = re.compile(args.pattern, re.MULTILINE)
        except re.error as exc:
            return f"grep error: invalid regex: {exc}"

        root = Path(args.path)
        if root.is_file():
            file_list = [root]
        elif root.is_dir():
            if args.glob:
                file_list = sorted(root.rglob(args.glob))
            else:
                file_list = sorted(f for f in root.rglob("*") if f.is_file())
        else:
            return f"grep error: {root} does not exist"

        def _scan() -> list[str]:
            results: list[str] = []
            for fpath in file_list:
                if not fpath.is_file():
                    continue
                try:
                    text = fpath.read_bytes().decode("utf-8", errors="replace")
                except OSError:
                    continue
                for lineno, line in enumerate(text.splitlines(), start=1):
                    if rx.search(line):
                        results.append(f"{fpath}:{lineno}:{line}")
                        if len(results) >= args.max_matches:
                            return results
            return results

        matches = await asyncio.get_event_loop().run_in_executor(None, _scan)
        if not matches:
            return f"grep: no matches for {args.pattern!r} under {root}"
        return "\n".join(matches)

    return FunctionTool(
        name="grep",
        description=(
            "Search file contents with a Python regex. "
            "Returns 'path:line:content' lines. "
            "Optionally restrict to files matching a glob pattern. "
            "Capped at `max_matches` (default 100)."
        ),
        input_model=_GrepArgs,
        handler=_h,
    )


# ── glob ──────────────────────────────────────────────────────────────────────


class _GlobArgs(BaseModel):
    model_config = {"extra": "forbid"}

    pattern: str = Field(..., description="Glob pattern for filenames (e.g. '**/*.py').")
    path: str = Field(".", description="Base directory for the glob.")


_GLOB_CAP = 1000


def glob_tool() -> Tool:
    """Pattern-based filename matching using pathlib.Path.glob.

    Returns a sorted list of matching paths, capped at 1000 entries.
    """

    async def _h(args: _GlobArgs) -> str:
        root = Path(args.path)
        if not root.exists():
            return f"glob error: {root} does not exist"
        if not root.is_dir():
            return f"glob error: {root} is not a directory"

        def _scan() -> list[str]:
            results: list[str] = []
            for p in sorted(root.glob(args.pattern)):
                results.append(str(p))
                if len(results) >= _GLOB_CAP:
                    break
            return results

        matches = await asyncio.get_event_loop().run_in_executor(None, _scan)
        if not matches:
            return f"glob: no paths matching {args.pattern!r} under {root}"
        result = "\n".join(matches)
        if len(matches) >= _GLOB_CAP:
            result += f"\n[...capped at {_GLOB_CAP} entries]"
        return result

    return FunctionTool(
        name="glob",
        description=(
            "Find paths matching a glob pattern under a base directory. "
            "Uses pathlib.Path.glob. Returns sorted paths, capped at 1000."
        ),
        input_model=_GlobArgs,
        handler=_h,
    )


# ── search_files (deprecated — delegates to grep) ────────────────────────────


class _SearchFilesArgs(BaseModel):
    model_config = {"extra": "forbid"}

    root: str = Field(".", description="Directory to search recursively.")
    pattern: str = Field(..., description="Glob pattern for filenames (e.g. '*.py').")
    contains: str | None = Field(
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
    """Recursively search for files matching a glob pattern, optionally filtering by content.

    DEPRECATED: prefer `grep` (regex content search) or `glob` (filename matching).
    This function is kept for back-compat with existing CodingAgent registrations.
    Internally delegates filename matching to pathlib glob and content filtering to
    a simple substring check (not regex), matching the original behaviour.
    """

    async def _h(args: _SearchFilesArgs) -> str:
        root = Path(args.root)
        if not root.exists():
            return f"search_files error: {root} does not exist"
        if not root.is_dir():
            return f"search_files error: {root} is not a directory"

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
            "DEPRECATED: prefer `grep` or `glob` instead. "
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
    cwd: str | None = Field(None, description="Working directory; defaults to process cwd.")


def shell_run_tool() -> Tool:
    """Run an arbitrary shell command via subprocess (NOT sandboxed by default).

    NOTE: This tool REQUIRES human approval before execution when registered
    inside CodingAgent's curated registry.  The approval wrapper is applied in
    coding_agent.py, not here.
    """

    async def _h(args: _ShellRunArgs) -> str:
        # Three-tier command gate: hardline patterns are unconditionally
        # refused; dangerous patterns require explicit operator approval
        # (we don't have a session_key here, so we conservatively block
        # them rather than auto-execute).
        from oxenclaw.security.command_gate import detect_command_threats

        verdict, label = detect_command_threats(args.command)
        if verdict == "hardline":
            return (
                f"shell error: BLOCKED (hardline) — {label}. "
                "This command is on the unconditional blocklist and "
                "cannot be executed via the agent."
            )
        if verdict == "dangerous":
            return (
                f"shell error: BLOCKED (dangerous) — {label}. "
                "This command needs explicit operator approval; use the "
                "dashboard's approve-command UI before retrying."
            )
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
        edit_tool(),
        list_dir_tool(),
        read_pdf_tool(),
        grep_tool(),
        glob_tool(),
        search_files_tool(),
        shell_run_tool(),
    ]


__all__ = [
    "coding_fs_tools",
    "edit_tool",
    "glob_tool",
    "grep_tool",
    "list_dir_tool",
    "read_file_tool",
    "read_pdf_tool",
    "search_files_tool",
    "shell_run_tool",
    "write_file_tool",
]
