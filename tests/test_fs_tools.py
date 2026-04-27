"""Tests for fs_tools.py — edit, read_file (enhanced), grep, glob, read_pdf."""

from __future__ import annotations

from pathlib import Path

from oxenclaw.tools_pkg.fs_tools import (
    edit_tool,
    glob_tool,
    grep_tool,
    read_file_tool,
    read_pdf_tool,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


# ── edit tool ────────────────────────────────────────────────────────────────


async def test_edit_replaces_unique_match(tmp_path: Path) -> None:
    """edit replaces exactly one occurrence when count=1."""
    p = tmp_path / "hello.py"
    _write(
        p,
        "line one\nline two\nline three\nline four\nline five\n",
    )
    tool = edit_tool()
    result = await tool.execute(
        {"path": str(p), "old_str": "line two", "new_str": "line TWO"}
    )
    assert "edited" in result
    assert "1 replacement(s)" in result
    content = p.read_text(encoding="utf-8")
    assert "line TWO" in content
    assert "line two" not in content


async def test_edit_rejects_count_mismatch(tmp_path: Path) -> None:
    """edit returns error and leaves file unchanged when actual count != expected count."""
    p = tmp_path / "dup.py"
    original = "foo\nfoo\nfoo\n"
    _write(p, original)
    tool = edit_tool()
    # Default count=1 but there are 3 occurrences.
    result = await tool.execute(
        {"path": str(p), "old_str": "foo", "new_str": "bar", "count": 1}
    )
    assert "error" in result.lower()
    assert "expected 1" in result
    assert "found 3" in result
    # File must be unchanged.
    assert p.read_text(encoding="utf-8") == original


async def test_edit_atomic_write_no_partial_on_error(tmp_path: Path) -> None:
    """Failure path (missing file) leaves no partial write."""
    missing = tmp_path / "nonexistent.py"
    tool = edit_tool()
    result = await tool.execute(
        {"path": str(missing), "old_str": "x", "new_str": "y"}
    )
    assert "error" in result.lower()
    assert not missing.exists()


async def test_edit_refuses_empty_old_str(tmp_path: Path) -> None:
    p = tmp_path / "f.txt"
    _write(p, "hello")
    tool = edit_tool()
    result = await tool.execute({"path": str(p), "old_str": "", "new_str": "x"})
    assert "error" in result.lower()
    assert "empty" in result.lower()


async def test_edit_refuses_noop(tmp_path: Path) -> None:
    p = tmp_path / "f.txt"
    _write(p, "hello")
    tool = edit_tool()
    result = await tool.execute({"path": str(p), "old_str": "hello", "new_str": "hello"})
    assert "error" in result.lower()
    assert "identical" in result.lower() or "no-op" in result.lower()


async def test_edit_rejects_binary_file(tmp_path: Path) -> None:
    p = tmp_path / "bin.bin"
    p.write_bytes(bytes(range(16)))
    tool = edit_tool()
    result = await tool.execute({"path": str(p), "old_str": "x", "new_str": "y"})
    assert "error" in result.lower()


async def test_edit_multiple_count(tmp_path: Path) -> None:
    """edit with count=3 replaces exactly 3 occurrences."""
    p = tmp_path / "multi.txt"
    _write(p, "x\nx\nx\n")
    tool = edit_tool()
    result = await tool.execute(
        {"path": str(p), "old_str": "x", "new_str": "z", "count": 3}
    )
    assert "3 replacement(s)" in result
    assert p.read_text(encoding="utf-8") == "z\nz\nz\n"


# ── read_file (enhanced) ─────────────────────────────────────────────────────


async def test_read_with_line_numbers_and_range(tmp_path: Path) -> None:
    """Lines 3..6 of a 10-line file are returned with the correct prefix shape."""
    lines = [f"line{i}\n" for i in range(1, 11)]
    p = tmp_path / "ten.txt"
    _write(p, "".join(lines))
    tool = read_file_tool()
    result = await tool.execute(
        {"path": str(p), "start_line": 3, "end_line": 6, "with_line_numbers": True}
    )
    # Should contain lines 3-6 only.
    assert "line3" in result
    assert "line6" in result
    assert "line1" not in result
    assert "line7" not in result
    # Prefix shape: 4-digit right-aligned number + │
    assert "   3│" in result
    assert "   6│" in result


async def test_read_without_line_numbers(tmp_path: Path) -> None:
    p = tmp_path / "f.txt"
    _write(p, "alpha\nbeta\n")
    tool = read_file_tool()
    result = await tool.execute({"path": str(p), "with_line_numbers": False})
    assert "│" not in result
    assert "alpha" in result


async def test_read_binary_returns_sentinel(tmp_path: Path) -> None:
    """Reading a binary file returns the sentinel string with hex sniff."""
    p = tmp_path / "rand.bin"
    data = bytes(range(16))
    p.write_bytes(data)
    tool = read_file_tool()
    result = await tool.execute({"path": str(p)})
    assert result.startswith("(binary file:")
    assert "bytes" in result
    assert "sniff=" in result


async def test_read_file_missing_returns_error(tmp_path: Path) -> None:
    tool = read_file_tool()
    result = await tool.execute({"path": str(tmp_path / "nope.txt")})
    assert "error" in result.lower()


async def test_read_file_full_content_default_line_numbers(tmp_path: Path) -> None:
    p = tmp_path / "abc.txt"
    _write(p, "abc\ndef\n")
    tool = read_file_tool()
    result = await tool.execute({"path": str(p)})
    assert "   1│" in result
    assert "abc" in result


# ── grep tool ────────────────────────────────────────────────────────────────


async def test_grep_regex_matches_across_files(tmp_path: Path) -> None:
    """grep finds a pattern in multiple files and returns path:line:content lines."""
    f1 = tmp_path / "a.py"
    f2 = tmp_path / "b.py"
    _write(f1, "hello world\nfoo bar\n")
    _write(f2, "hello again\nbaz\n")
    tool = grep_tool()
    result = await tool.execute({"pattern": "hello", "path": str(tmp_path)})
    assert "a.py" in result
    assert "b.py" in result
    assert "hello world" in result
    assert "hello again" in result


async def test_grep_glob_filter(tmp_path: Path) -> None:
    """grep with glob='*.py' skips non-.py files."""
    _write(tmp_path / "code.py", "needle")
    _write(tmp_path / "notes.txt", "needle")
    tool = grep_tool()
    result = await tool.execute({"pattern": "needle", "path": str(tmp_path), "glob": "*.py"})
    assert "code.py" in result
    assert "notes.txt" not in result


async def test_grep_no_match(tmp_path: Path) -> None:
    _write(tmp_path / "x.py", "nothing here")
    tool = grep_tool()
    result = await tool.execute({"pattern": "NOTFOUND", "path": str(tmp_path)})
    assert "no matches" in result.lower()


async def test_grep_invalid_regex(tmp_path: Path) -> None:
    tool = grep_tool()
    result = await tool.execute({"pattern": "[invalid", "path": str(tmp_path)})
    assert "error" in result.lower()
    assert "regex" in result.lower()


async def test_grep_single_file(tmp_path: Path) -> None:
    p = tmp_path / "single.txt"
    _write(p, "match me\nno\n")
    tool = grep_tool()
    result = await tool.execute({"pattern": "match", "path": str(p)})
    assert "match me" in result


# ── glob tool ────────────────────────────────────────────────────────────────


async def test_glob_returns_matching_paths(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "c.txt").write_text("")
    tool = glob_tool()
    result = await tool.execute({"pattern": "*.py", "path": str(tmp_path)})
    assert "a.py" in result
    assert "b.py" in result
    assert "c.txt" not in result


async def test_glob_returns_capped_list(tmp_path: Path) -> None:
    """glob caps at 1000 entries even when more files exist."""
    sub = tmp_path / "many"
    sub.mkdir()
    for i in range(1050):
        (sub / f"file_{i:04d}.txt").write_text("")
    tool = glob_tool()
    result = await tool.execute({"pattern": "*.txt", "path": str(sub)})
    lines = [l for l in result.splitlines() if l and not l.startswith("[")]
    assert len(lines) == 1000
    assert "capped at 1000" in result


async def test_glob_no_match(tmp_path: Path) -> None:
    tool = glob_tool()
    result = await tool.execute({"pattern": "*.xyz", "path": str(tmp_path)})
    assert "no paths" in result.lower()


async def test_glob_missing_dir(tmp_path: Path) -> None:
    tool = glob_tool()
    result = await tool.execute({"pattern": "*.py", "path": str(tmp_path / "nosuchdir")})
    assert "error" in result.lower()


async def test_glob_recursive_pattern(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "deep.py").write_text("")
    tool = glob_tool()
    result = await tool.execute({"pattern": "**/*.py", "path": str(tmp_path)})
    assert "deep.py" in result


# ── read_pdf tool ─────────────────────────────────────────────────────────────


async def test_read_pdf_missing_file(tmp_path: Path) -> None:
    tool = read_pdf_tool()
    result = await tool.execute({"path": str(tmp_path / "missing.pdf")})
    assert "error" in result.lower()


async def test_read_pdf_pypdf_availability(tmp_path: Path) -> None:
    """Verify read_pdf behaves correctly whether or not pypdf is available."""
    tool = read_pdf_tool()
    # Use a non-PDF file; if pypdf is available it will error on open, not on import.
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"not a real pdf")
    try:
        import pypdf  # noqa: F401
        pypdf_available = True
    except ImportError:
        pypdf_available = False

    result = await tool.execute({"path": str(fake)})
    if pypdf_available:
        # Should return an error about reading the PDF, not an ImportError.
        assert "error" in result.lower()
        assert "pip install" not in result
    else:
        # Should return the not-installed message.
        assert "not installed" in result.lower()
        assert "pip install pypdf" in result
