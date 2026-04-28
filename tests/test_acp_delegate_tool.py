"""Tests for the `delegate_to_acp` tool — PiAgent → frontier ACP.

The tool spawns a child ACP server and runs a single turn through
it. We exercise it against `python -m oxenclaw.acp.server --backend
fake` so the test is fully self-contained — no external CLI
required, just the python interpreter that's already running pytest.
"""

from __future__ import annotations

import sys

from oxenclaw.tools_pkg.acp_delegate_tool import acp_delegate_tool


def _argv_to_loopback() -> list[str]:
    return [
        sys.executable,
        "-m",
        "oxenclaw.acp.server",
        "--backend",
        "fake",
    ]


async def test_delegate_to_acp_loopback_returns_echoed_text() -> None:
    """Spawn the in-tree fake ACP server and confirm a delegation
    round-trip: the child echoes the prompt as one text_delta + a
    done(stop), and the tool surfaces both."""
    tool = acp_delegate_tool()
    result = await tool.execute(
        {
            "runtime": "custom",
            "argv": _argv_to_loopback(),
            "prompt": "delegate this please",
        }
    )
    assert "stopReason=stop" in result
    assert "delegate this please" in result


async def test_delegate_to_acp_unknown_binary_returns_friendly_error() -> None:
    tool = acp_delegate_tool()
    result = await tool.execute(
        {
            "runtime": "custom",
            "argv": ["definitely-not-a-real-binary-xyz123", "acp"],
            "prompt": "x",
        }
    )
    assert "delegate_to_acp" in result
    # Either FileNotFoundError or a wire/general error — both must
    # be surfaced as a tool-result string, not a raised exception.
    assert "[" in result and "]" in result


async def test_delegate_to_acp_custom_requires_argv() -> None:
    tool = acp_delegate_tool()
    # Pydantic model validation is happy (argv is optional), but the
    # handler raises ValueError when runtime='custom' without argv —
    # which the tool surfaces as a failure string.
    result = await tool.execute({"runtime": "custom", "prompt": "x"})
    assert "failed" in result.lower() or "argv" in result.lower()


async def test_delegate_to_acp_known_runtime_falls_back_when_cli_missing() -> None:
    """If `claude` / `codex` / `gemini` aren't installed on the host,
    the tool must still return a friendly error string instead of
    crashing the parent agent's turn."""
    tool = acp_delegate_tool()
    # Neither claude nor codex nor gemini is guaranteed installed in
    # CI, so any of these should produce a friendly error if missing.
    result = await tool.execute({"runtime": "claude", "prompt": "test", "timeout_seconds": 5})
    # Pass criterion: tool returned a string starting with "[delegate"
    # rather than raising. The string content depends on the host
    # — installed → real text; missing → friendly error.
    assert isinstance(result, str)
    assert result.startswith("[delegate") or "delegate" in result


async def test_delegate_tool_is_registered_in_default_bundle() -> None:
    """Ensure the tool is wired into the default bundle so every
    gateway-launched agent sees it. Regression catcher: if a future
    refactor drops `acp_delegate_tool()` from `bundle.py`, this
    catches it."""
    from oxenclaw.tools_pkg.bundle import default_bundled_tools

    names = {t.name for t in default_bundled_tools()}
    assert "delegate_to_acp" in names, (
        f"delegate_to_acp must ship in the default bundled tools — got: {sorted(names)}"
    )
