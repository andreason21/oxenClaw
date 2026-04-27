"""ACP harness spawn — drive Claude/Codex/Gemini CLI as a child agent."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from oxenclaw.agents.acp_subprocess import resolve_cli, spawn_acp


def test_resolve_cli_returns_none_for_missing_binary() -> None:
    assert resolve_cli("claude", "definitely-not-installed-xyz123") is None


async def test_spawn_returns_error_for_missing_cli() -> None:
    result = await spawn_acp(
        runtime="claude",
        prompt="hello",
        cli_override="not-a-real-binary-xyzzy",
    )
    assert not result.ok
    assert result.error is not None
    assert "not found on PATH" in result.error
    assert result.exit_code == -1


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only fake binary test")
async def test_spawn_uses_arg_mode_with_real_binary(tmp_path: Path) -> None:
    """Use `/bin/echo` as a stand-in for an ACP CLI to verify the
    arg-mode argv shape and stdout capture."""
    result = await spawn_acp(
        runtime="claude",
        prompt="hello world",
        cli_override="echo",
    )
    assert result.ok
    assert "hello world" in result.stdout
    assert result.exit_code == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only fake binary test")
async def test_spawn_stdin_mode_pipes_prompt() -> None:
    """`/bin/cat` echoes stdin → confirms stdin-mode pipe path."""
    result = await spawn_acp(
        runtime="gemini",
        prompt="piped through stdin",
        cli_override="cat",
        stdin_mode="stdin",
    )
    assert result.ok
    assert "piped through stdin" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only fake binary test")
async def test_spawn_timeout_kills_long_process() -> None:
    """A CLI that hangs on stdin gets killed after the timeout.
    Use `cat` in stdin-mode but never close stdin (we send a
    no-newline prompt and let the timeout fire)."""
    # `cat` reads stdin until EOF — pass a tiny payload via stdin
    # mode and close stdin immediately; cat exits fast. So we use
    # `sleep 5` and stdin-mode (sleep ignores stdin) to ensure the
    # process truly waits past the timeout.
    result = await spawn_acp(
        runtime="codex",
        prompt="",  # not used in stdin mode below since we extra_args
        cli_override="sleep",
        extra_args=["5"],
        stdin_mode="stdin",  # bypasses the prompt-as-arg path
        timeout_seconds=0.2,
    )
    assert result.timed_out
    assert not result.ok
