"""Walker traversal + hash stability."""

from __future__ import annotations

from pathlib import Path

from sampyclaw.memory.walker import scan_memory_dir


def test_missing_dir_yields_empty(tmp_path: Path) -> None:
    out = list(scan_memory_dir(tmp_path / "does-not-exist"))
    assert out == []


def test_skips_hidden_files_and_dirs(tmp_path: Path) -> None:
    (tmp_path / "visible.md").write_text("a")
    (tmp_path / ".hidden.md").write_text("b")
    sub = tmp_path / ".cache"
    sub.mkdir()
    (sub / "x.md").write_text("c")
    rels = [r for r, *_ in scan_memory_dir(tmp_path)]
    assert "visible.md" in rels
    assert all(not p.startswith(".") for p in rels)
    assert all("/.cache" not in p and not p.startswith(".cache") for p in rels)


def test_subdirectories_are_traversed(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "deeper"
    nested.mkdir(parents=True)
    (nested / "note.md").write_text("# hi")
    rels = [r for r, *_ in scan_memory_dir(tmp_path)]
    assert any("deep" in r and "note.md" in r for r in rels)


def test_content_hash_stable_across_calls(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("hello world")
    h1 = next(scan_memory_dir(tmp_path))[4]
    h2 = next(scan_memory_dir(tmp_path))[4]
    assert h1 == h2


def test_only_markdown_files_yielded(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("md")
    (tmp_path / "b.txt").write_text("txt")
    rels = [r for r, *_ in scan_memory_dir(tmp_path)]
    assert rels == ["a.md"]


def test_max_depth_caps_descent(tmp_path: Path) -> None:
    # depth 3 from root → 4 path parts (deep1, deep2, deep3, leaf.md).
    deep = tmp_path / "deep1" / "deep2" / "deep3"
    deep.mkdir(parents=True)
    (deep / "leaf.md").write_text("x")
    (tmp_path / "shallow.md").write_text("y")
    rels = [r for r, *_ in scan_memory_dir(tmp_path, max_depth=2)]
    assert "shallow.md" in rels
    assert all("leaf.md" not in r for r in rels)


def test_oversized_file_skipped(tmp_path: Path) -> None:
    (tmp_path / "small.md").write_text("ok")
    (tmp_path / "big.md").write_text("x" * 200)
    rels = [r for r, *_ in scan_memory_dir(tmp_path, max_file_size_bytes=100)]
    assert "small.md" in rels
    assert "big.md" not in rels


def test_symlink_outside_root_skipped(tmp_path: Path) -> None:
    outside = tmp_path.parent / "_outside_walker_test"
    outside.mkdir(exist_ok=True)
    try:
        (outside / "secret.md").write_text("oops")
        link = tmp_path / "linked.md"
        try:
            link.symlink_to(outside / "secret.md")
        except (OSError, NotImplementedError):
            import pytest

            pytest.skip("symlink not supported on this platform")
        rels = [r for r, *_ in scan_memory_dir(tmp_path)]
        assert "linked.md" not in rels
    finally:
        import shutil

        shutil.rmtree(outside, ignore_errors=True)
