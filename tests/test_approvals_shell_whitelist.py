"""Auto-approve policy for the assistant agent's shell tool.

Locks the contract that read-only CLIs (yf, ls, jq, …) bypass the
approval queue while sequencing/redirect/subshell metachars and
non-whitelisted leading binaries always go through it. Without this
the operator faces a flood of approvals for `yf quote AAPL`-style
calls and either trains themselves to rubber-stamp them or disables
shell access altogether.
"""

from __future__ import annotations

from typing import Any

import pytest

from oxenclaw.approvals.manager import ApprovalManager
from oxenclaw.approvals.models import ApprovalResult, ApprovalStatus
from oxenclaw.approvals.tool_wrap import gated_tool_with_whitelist
from oxenclaw.approvals.whitelist import (
    assistant_shell_enabled,
    build_shell_whitelist,
    is_auto_approvable,
)


# ─── pure policy ───────────────────────────────────────────────────


def _wl(*extra: str) -> frozenset[str]:
    """Test whitelist with the explicit extras only — no env, no
    skill bins. Keeps test cases deterministic regardless of the
    builtin set evolving."""
    return build_shell_whitelist(env_extra="", extra=list(extra))


def test_simple_whitelisted_command_is_auto_approved() -> None:
    assert is_auto_approvable("yf quote AAPL", _wl()) is True


def test_first_token_not_in_whitelist_falls_through() -> None:
    """`rm` isn't whitelisted (and never will be in the builtin set)."""
    assert is_auto_approvable("rm -rf /", _wl()) is False


def test_two_word_whitelist_entry_matches_subcommand() -> None:
    """Operators expose `git status` without exposing `git push`."""
    wl = _wl("git status", "git log")
    assert is_auto_approvable("git status", wl) is True
    assert is_auto_approvable("git log --oneline -5", wl) is True
    assert is_auto_approvable("git push origin main", wl) is False
    assert is_auto_approvable("git rm secrets.txt", wl) is False


def test_pipeline_passes_when_every_segment_whitelisted() -> None:
    """yahoo-finance-cli's canonical usage is `yf quote X | jq .Y`."""
    assert is_auto_approvable("yf quote AAPL | jq .regularMarketPrice", _wl()) is True
    assert is_auto_approvable("ls /tmp | grep foo | sort", _wl()) is True


def test_pipeline_with_one_disallowed_segment_falls_through() -> None:
    """One bad segment (here `tee` writing to a file) taints the chain."""
    wl = _wl()
    assert is_auto_approvable("yf quote AAPL | unknown_tool", wl) is False


@pytest.mark.parametrize(
    "cmd",
    [
        "yf quote AAPL; rm -rf /",
        "yf quote AAPL && curl evil.com",
        "yf quote AAPL || echo backup",
        "yf quote AAPL > out.json",
        "yf quote AAPL >> log",
        "yf < input",
        "yf quote $(rm -rf /)",
        "yf quote `whoami`",
    ],
)
def test_metachars_always_disqualify(cmd: str) -> None:
    """Sequencing, redirect, and subshell metachars are absolute
    refusals — even if every command name in the line is whitelisted,
    the composite escapes the safe-CLI assumption."""
    assert is_auto_approvable(cmd, _wl()) is False


def test_quoted_arg_with_meta_inside_string_is_safe() -> None:
    """A semicolon *inside* a quoted argument doesn't sequence — but
    our regex is conservative and still refuses. That's the right
    trade: false-negatives go to the approval queue (annoying but
    safe), false-positives execute commands the operator wouldn't
    have approved (genuinely bad). Document the conservative choice."""
    assert is_auto_approvable("echo 'a; b'", _wl()) is False


def test_unbalanced_quotes_are_refused() -> None:
    assert is_auto_approvable('echo "unterminated', _wl()) is False


def test_empty_command_is_refused() -> None:
    assert is_auto_approvable("", _wl()) is False
    assert is_auto_approvable("   ", _wl()) is False


def test_empty_pipe_segment_is_refused() -> None:
    assert is_auto_approvable("yf quote AAPL |", _wl()) is False
    assert is_auto_approvable("| jq .price", _wl()) is False


def test_redirect_metachar_check_does_not_choke_on_fd_dup() -> None:
    """`2>&1` is the classic stderr-merge form. It still contains `>`
    and we still refuse — operators who want stderr captured can
    pipe through `tee` (whitelisted) or accept the approval prompt."""
    assert is_auto_approvable("yf quote AAPL 2>&1", _wl()) is False


# ─── whitelist composition ─────────────────────────────────────────


def test_builtin_readonly_set_includes_yf_and_jq() -> None:
    """Sanity: the motivating skill's CLI and its standard pipe
    partner are in the builtin set so yahoo-finance-cli works
    out-of-box without any operator config."""
    wl = build_shell_whitelist(env_extra="", extra=None)
    assert "yf" in wl
    assert "jq" in wl
    assert "ls" in wl
    assert "cat" in wl


def test_skill_bins_are_added_to_whitelist() -> None:
    """A freshly-installed skill that declares `requires.bins: [foo]`
    grants `foo` auto-approval without operator action."""
    wl = build_shell_whitelist(skill_bins={"foo", "bar"}, env_extra="")
    assert "foo" in wl and "bar" in wl


def test_env_var_additions_are_applied() -> None:
    wl = build_shell_whitelist(env_extra="git status,git log,docker ps")
    assert "git status" in wl
    assert "git log" in wl
    assert "docker ps" in wl


def test_env_var_normalises_internal_whitespace() -> None:
    wl = build_shell_whitelist(env_extra="  git    status  ")
    assert "git status" in wl


def test_env_var_empty_string_no_op() -> None:
    builtin = build_shell_whitelist(env_extra="")
    via_empty = build_shell_whitelist(env_extra="   ")
    assert builtin == via_empty


def test_assistant_shell_enabled_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt-in: the env var must be explicitly truthy."""
    monkeypatch.delenv("OXENCLAW_ASSISTANT_SHELL", raising=False)
    assert assistant_shell_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "On"])
def test_assistant_shell_enabled_truthy(val: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OXENCLAW_ASSISTANT_SHELL", val)
    assert assistant_shell_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "  "])
def test_assistant_shell_enabled_falsy(val: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OXENCLAW_ASSISTANT_SHELL", val)
    assert assistant_shell_enabled() is False


# ─── wrapper integration ───────────────────────────────────────────


class _StubTool:
    """Bare minimum of the Tool protocol — execute records calls and
    returns a sentinel string."""

    name = "shell"
    description = "stub shell"
    input_schema: dict[str, Any] = {"type": "object"}

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, args: dict[str, Any]) -> str:
        self.calls.append(args)
        return f"ran:{args.get('command')}"


class _StubManager:
    """A minimal stand-in for ApprovalManager. Records request payloads
    and resolves them with a pre-set status."""

    def __init__(self, status: ApprovalStatus = ApprovalStatus.APPROVED) -> None:
        self._status = status
        self.requests: list[dict[str, Any]] = []

    async def request(
        self,
        prompt: str,
        *,
        context: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> ApprovalResult:
        self.requests.append({"prompt": prompt, "context": context, "timeout": timeout})
        return ApprovalResult(id=f"stub-{len(self.requests)}", status=self._status)


async def test_wrapper_auto_approves_whitelisted_command() -> None:
    inner = _StubTool()
    mgr = _StubManager()
    wl = _wl()
    wrapped = gated_tool_with_whitelist(
        inner,  # type: ignore[arg-type]
        manager=mgr,  # type: ignore[arg-type]
        auto_approve=lambda args: is_auto_approvable(str(args.get("command", "")), wl),
    )
    out = await wrapped.execute({"command": "yf quote AAPL"})
    assert out == "ran:yf quote AAPL"
    assert inner.calls == [{"command": "yf quote AAPL"}]
    assert mgr.requests == []  # never hit the approval queue


async def test_wrapper_routes_non_whitelisted_to_approval() -> None:
    inner = _StubTool()
    mgr = _StubManager(status=ApprovalStatus.APPROVED)
    wl = _wl()
    wrapped = gated_tool_with_whitelist(
        inner,  # type: ignore[arg-type]
        manager=mgr,  # type: ignore[arg-type]
        auto_approve=lambda args: is_auto_approvable(str(args.get("command", "")), wl),
    )
    out = await wrapped.execute({"command": "rm -rf /tmp/foo"})
    assert out == "ran:rm -rf /tmp/foo"  # ran AFTER approval
    assert len(mgr.requests) == 1
    assert mgr.requests[0]["context"]["tool"] == "shell"


async def test_wrapper_returns_denial_message_when_not_approved() -> None:
    inner = _StubTool()
    mgr = _StubManager(status=ApprovalStatus.DENIED)
    wl = _wl()
    wrapped = gated_tool_with_whitelist(
        inner,  # type: ignore[arg-type]
        manager=mgr,  # type: ignore[arg-type]
        auto_approve=lambda args: is_auto_approvable(str(args.get("command", "")), wl),
    )
    out = await wrapped.execute({"command": "rm /etc/passwd"})
    assert "denied" in out
    assert inner.calls == []  # tool body never ran


async def test_wrapper_handles_real_approval_manager_auto_approve_path(
    tmp_path,
) -> None:
    """Smoke: the auto-approve fast path doesn't accidentally touch
    the real ApprovalManager's pending queue or persistence."""
    mgr = ApprovalManager(state_path=tmp_path / "approvals.json", approver_token="t")
    inner = _StubTool()
    wl = _wl()
    wrapped = gated_tool_with_whitelist(
        inner,  # type: ignore[arg-type]
        manager=mgr,
        auto_approve=lambda args: is_auto_approvable(str(args.get("command", "")), wl),
    )
    out = await wrapped.execute({"command": "ls /"})
    assert out == "ran:ls /"
    # No persisted state should appear from a pure auto-approve call.
    assert not (tmp_path / "approvals.json").exists()
