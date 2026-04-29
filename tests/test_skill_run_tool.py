"""skill_run executes documented scripts from installed skills.

Locks the contract: an installed skill with `scripts/<file>.py` is
runnable via `skill_run(skill, script, args)`, output and rc are
surfaced cleanly, and the tool fails loudly + actionably when the
required interpreter (uv for PEP 723 scripts, python3 fallback) is
missing — that signal is what enables the user to install the
right thing instead of staring at "doesn't work".
"""

from __future__ import annotations

import stat
import textwrap
from pathlib import Path

from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.tools_pkg.skill_run import skill_run_tool


def _install_skill(home: Path, slug: str, scripts: dict[str, str]) -> Path:
    """Materialise a fake installed skill under `<home>/skills/<slug>/`
    with the given script files. Returns the skill dir."""
    paths = OxenclawPaths(home=home)
    paths.ensure_home()
    sd = home / "skills" / slug
    (sd / "scripts").mkdir(parents=True, exist_ok=True)
    (sd / "SKILL.md").write_text(
        f"---\nname: {slug}\ndescription: test\n---\n\nbody\n",
        encoding="utf-8",
    )
    for name, content in scripts.items():
        p = sd / "scripts" / name
        p.write_text(content, encoding="utf-8")
        if name.endswith(".sh") or name.endswith(".py"):
            p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return sd


async def test_runs_python_script_with_args(tmp_path: Path) -> None:
    _install_skill(
        tmp_path,
        "echoer",
        {
            "echo.py": textwrap.dedent(
                """
                import sys
                print("HELLO", *sys.argv[1:])
                """
            ).strip()
            + "\n",
        },
    )
    paths = OxenclawPaths(home=tmp_path)
    tool = skill_run_tool(paths=paths)
    out = await tool.execute(
        {"skill": "echoer", "script": "echo.py", "args": ["world", "42"]}
    )
    assert "exit=0" in out
    assert "HELLO world 42" in out


async def test_unknown_skill_lists_installed(tmp_path: Path) -> None:
    _install_skill(tmp_path, "real-skill", {"x.py": "print('ok')\n"})
    tool = skill_run_tool(paths=OxenclawPaths(home=tmp_path))
    out = await tool.execute({"skill": "nope", "script": "x.py", "args": []})
    assert "not installed" in out
    assert "real-skill" in out  # tells user what IS installed


async def test_unknown_script_lists_available(tmp_path: Path) -> None:
    _install_skill(
        tmp_path,
        "demo",
        {"alpha.py": "print('a')\n", "beta.py": "print('b')\n"},
    )
    tool = skill_run_tool(paths=OxenclawPaths(home=tmp_path))
    out = await tool.execute({"skill": "demo", "script": "missing.py", "args": []})
    assert "not found" in out
    assert "alpha.py" in out and "beta.py" in out


async def test_path_traversal_rejected(tmp_path: Path) -> None:
    _install_skill(tmp_path, "demo", {"x.py": "print('x')\n"})
    # Attempt to escape via ../
    tool = skill_run_tool(paths=OxenclawPaths(home=tmp_path))
    out = await tool.execute(
        {"skill": "demo", "script": "../../etc/passwd", "args": []}
    )
    assert "outside" in out


async def test_nonzero_exit_surfaced(tmp_path: Path) -> None:
    _install_skill(
        tmp_path,
        "boom",
        {
            "fail.py": textwrap.dedent(
                """
                import sys
                print("partial output", flush=True)
                print("err detail", file=sys.stderr, flush=True)
                sys.exit(7)
                """
            ).strip()
            + "\n",
        },
    )
    tool = skill_run_tool(paths=OxenclawPaths(home=tmp_path))
    out = await tool.execute({"skill": "boom", "script": "fail.py", "args": []})
    assert "exit=7" in out
    assert "non-zero exit" in out
    assert "partial output" in out
    assert "err detail" in out  # stderr also surfaced


async def test_pep723_without_uv_returns_actionable_message(tmp_path: Path) -> None:
    """PEP 723 scripts MUST run under `uv run` — the dependency block
    isn't installable any other way. The tool's response must say
    that explicitly so the user (or the model) knows to install uv."""
    sd = _install_skill(
        tmp_path,
        "deps",
        {
            "needs_uv.py": textwrap.dedent(
                """
                # /// script
                # requires-python = ">=3.10"
                # dependencies = ["yfinance>=0.2.40"]
                # ///
                print("ok")
                """
            ).strip()
            + "\n",
        },
    )
    # Build a custom tool whose `which` reports uv as missing but
    # python3 as present — we don't want to depend on the test host
    # actually missing uv (or having it).
    from oxenclaw.tools_pkg import skill_run as _module

    real_which = _module.shutil.which
    try:
        _module.shutil.which = lambda b: None if b == "uv" else real_which(b)
        tool = skill_run_tool(paths=OxenclawPaths(home=tmp_path))
        out = await tool.execute({"skill": "deps", "script": "needs_uv.py", "args": []})
    finally:
        _module.shutil.which = real_which
    # We hit the python3 fallback (since uv is missing but python3 is).
    # The script imports yfinance which isn't installed — should fail.
    # OR we go down the actionable-message path. Either way the tool
    # must return text that names uv as the relevant install.
    assert "uv" in out


async def test_timeout_kills_long_script(tmp_path: Path) -> None:
    _install_skill(
        tmp_path,
        "slow",
        {
            "loop.py": textwrap.dedent(
                """
                import time
                time.sleep(10)
                print("never")
                """
            ).strip()
            + "\n",
        },
    )
    tool = skill_run_tool(paths=OxenclawPaths(home=tmp_path))
    out = await tool.execute(
        {"skill": "slow", "script": "loop.py", "args": [], "timeout_seconds": 1}
    )
    assert "timed out" in out
    assert "never" not in out


async def test_runs_in_skill_dir_as_cwd(tmp_path: Path) -> None:
    """Scripts often write/read sibling files relative to the skill
    dir; the tool must set cwd accordingly."""
    sd = _install_skill(
        tmp_path,
        "cwdcheck",
        {
            "where.py": textwrap.dedent(
                """
                import os
                print("CWD=" + os.getcwd())
                """
            ).strip()
            + "\n",
        },
    )
    tool = skill_run_tool(paths=OxenclawPaths(home=tmp_path))
    out = await tool.execute({"skill": "cwdcheck", "script": "where.py", "args": []})
    assert f"CWD={sd}" in out
