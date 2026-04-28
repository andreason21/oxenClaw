"""CodingAgent — PiAgent subclass focused on file editing, planning, and shell.

Mirrors the role of openclaw's `pi-coding-agent` / `pi-embedded-runner` stack
for the coding specialisation. Ships as a *minimum-viable* skeleton this session;
see `docs/CODING_AGENT.md` for the full design and what is deferred to the next
session.

Key differences from plain PiAgent
------------------------------------
1. **Curated tool registry** — only file-system + shell tools.  The general
   weather / web / github / skill_creator bundle is intentionally excluded to
   keep the model's attention on the coding task.
2. **Plan-first system prompt** — the LLM is instructed to emit a `<plan>`
   block before making file edits, and to confirm each edit step before
   proceeding.
3. **Approval gating** — write_file and shell tools are wrapped with
   `gated_tool()` when an `ApprovalManager` is injected.  Read-only tools
   (read_file, list_dir, search_files) are never gated.

Usage (direct construction)::

    from oxenclaw.agents.coding_agent import CodingAgent
    agent = CodingAgent(agent_id="coding", model_id="claude-sonnet-4-6")

Usage via factory::

    from oxenclaw.agents.factory import build_agent
    agent = build_agent(agent_id="c", provider="anthropic", agent_type="coding")
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from oxenclaw.agents.pi_agent import PiAgent
from oxenclaw.agents.tools import ToolRegistry
from oxenclaw.config.paths import OxenclawPaths
from oxenclaw.tools_pkg.fs_tools import (
    edit_tool,
    glob_tool,
    grep_tool,
    list_dir_tool,
    read_file_tool,
    read_pdf_tool,
    shell_run_tool,
    write_file_tool,
)
from oxenclaw.tools_pkg.process_tool import process_tool
from oxenclaw.tools_pkg.update_plan_tool import update_plan_tool

if TYPE_CHECKING:
    from oxenclaw.approvals.manager import ApprovalManager
    from oxenclaw.pi.session import SessionManager

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

CODING_SYSTEM_PROMPT = """\
You are a coding agent. Before making any changes, call `update_plan` with \
the full list of steps you intend to take — this replaces the freeform \
`<plan>` text block and lets the dashboard show live progress.

Call `update_plan` again after each step to flip its status to \
`in_progress`, `completed`, or `blocked`.  Pass the **complete** step list \
every time (the tool overwrites the previous plan atomically).

Available tools:
- **read_file** / **list_dir** / **grep** / **glob** — safe, no approval needed.
- **update_plan** — record or update the structured step plan; no approval needed.
- **edit** — targeted string-replace; requires human approval.
- **write_file** — creates or overwrites a file; requires human approval.
- **read_pdf** — extract text from a PDF; no approval needed.
- **shell** — runs a shell command; requires human approval.
- **process** — start a persistent background process (dev server, REPL, watcher) \
and interact with it via send_keys / read_output / stop; requires human approval.

File-edit conventions
- **Prefer `edit` over `write_file` for modifications.** `edit` is the most \
token-efficient file-modifier: supply the exact `old_str` you want replaced, \
the `new_str` to put in its place, and the expected number of occurrences \
(`count`). This avoids sending the entire file content over the wire and \
eliminates the risk of accidentally overwriting unrelated sections.
- Use `write_file` only when creating a new file from scratch or when a \
full rewrite is genuinely needed.
- **`read_file` supports line ranges** via `start_line` / `end_line`, which \
is useful for inspecting large files without loading the whole content. \
Lines are prefixed with 4-digit right-aligned numbers and a │ separator \
(e.g. `   1│ first line`) when `with_line_numbers=True` (the default).
- Always state which file you are editing and why, before calling edit or \
write_file.
- If a shell command modifies state (installs a package, runs tests, \
deletes files) explain the rationale before calling it.

Approval protocol
- Approval-gated tools will block until the operator approves or denies.
- If a tool call is denied, acknowledge it, explain the impact, and ask \
the user how to proceed.
- Never retry a denied tool call in the same form without user consent.

When you are done, summarise every file changed and every shell command run.
"""


# ---------------------------------------------------------------------------
# CodingAgent
# ---------------------------------------------------------------------------


class CodingAgent(PiAgent):
    """PiAgent specialised for coding tasks (file edit / plan / shell).

    Parameters
    ----------
    approval_manager:
        Optional `ApprovalManager` from `oxenclaw.approvals`. When provided,
        write_file and shell are wrapped with `gated_tool()` so every
        state-mutating call blocks until the operator approves or denies it.
        Read-only tools are never gated.

    All other parameters are forwarded to `PiAgent.__init__` unchanged.
    """

    def __init__(
        self,
        *,
        agent_id: str = "coding",
        system_prompt: str = CODING_SYSTEM_PROMPT,
        tools: ToolRegistry | None = None,
        approval_manager: ApprovalManager | None = None,
        session_manager: SessionManager | None = None,
        paths: OxenclawPaths | None = None,
        **kwargs,
    ) -> None:
        if tools is None:
            tools = _build_coding_registry(
                approval_manager, paths=paths, session_manager=session_manager
            )

        super().__init__(
            agent_id=agent_id,
            system_prompt=system_prompt,
            tools=tools,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# Registry builder
# ---------------------------------------------------------------------------


def _build_coding_registry(
    approval_manager: ApprovalManager | None = None,
    *,
    paths: OxenclawPaths | None = None,
    session_manager: SessionManager | None = None,
) -> ToolRegistry:
    """Build the curated ToolRegistry for CodingAgent.

    Read-only tools: never gated.
    Write / shell tools: wrapped with gated_tool() when approval_manager is set.
    update_plan: always ungated — it writes plan metadata, not user files.
    """
    reg = ToolRegistry()

    # Read-only — always ungated.
    reg.register(read_file_tool())
    reg.register(read_pdf_tool())
    reg.register(list_dir_tool())
    reg.register(grep_tool())
    reg.register(glob_tool())

    # Plan tracking — always ungated.
    reg.register(update_plan_tool(paths=paths))

    # Write-side + process — conditionally gated.
    raw_write = write_file_tool()
    raw_edit = edit_tool()
    raw_shell = shell_run_tool()
    raw_process = process_tool()

    if approval_manager is not None:
        from oxenclaw.approvals.tool_wrap import gated_tool

        reg.register(gated_tool(raw_write, manager=approval_manager))
        reg.register(gated_tool(raw_edit, manager=approval_manager))
        reg.register(gated_tool(raw_shell, manager=approval_manager))
        reg.register(gated_tool(raw_process, manager=approval_manager))
    else:
        reg.register(raw_write)
        reg.register(raw_edit)
        reg.register(raw_shell)
        reg.register(raw_process)

    # Session tools — read-only always ungated; mutating gated when
    # approval_manager is supplied.
    if session_manager is not None:
        from oxenclaw.tools_pkg.session_tools import build_session_tools

        for t in build_session_tools(session_manager, approval_manager=approval_manager):
            reg.register(t)

    return reg


__all__ = ["CODING_SYSTEM_PROMPT", "CodingAgent"]
