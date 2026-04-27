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
from oxenclaw.tools_pkg.fs_tools import (
    list_dir_tool,
    read_file_tool,
    search_files_tool,
    shell_run_tool,
    write_file_tool,
)

if TYPE_CHECKING:
    from oxenclaw.approvals.manager import ApprovalManager

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

CODING_SYSTEM_PROMPT = """\
You are a coding agent. Before making any changes, always produce a \
`<plan>` block listing the steps you intend to take:

<plan>
1. …
2. …
</plan>

Then execute each step in order, using the available tools:
- **read_file** / **list_dir** / **search_files** — safe, no approval needed.
- **write_file** — creates or overwrites a file; requires human approval.
- **shell** — runs a shell command; requires human approval.

File-edit conventions
- Prefer targeted writes: read the file first, then write back the full \
updated content.
- Always state which file you are editing and why, before calling write_file.
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
        approval_manager: "ApprovalManager | None" = None,
        **kwargs,
    ) -> None:
        if tools is None:
            tools = _build_coding_registry(approval_manager)

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
    approval_manager: "ApprovalManager | None" = None,
) -> ToolRegistry:
    """Build the curated ToolRegistry for CodingAgent.

    Read-only tools: never gated.
    Write / shell tools: wrapped with gated_tool() when approval_manager is set.
    """
    reg = ToolRegistry()

    # Read-only — always ungated.
    reg.register(read_file_tool())
    reg.register(list_dir_tool())
    reg.register(search_files_tool())

    # Write-side — conditionally gated.
    raw_write = write_file_tool()
    raw_shell = shell_run_tool()

    if approval_manager is not None:
        from oxenclaw.approvals.tool_wrap import gated_tool

        reg.register(gated_tool(raw_write, manager=approval_manager))
        reg.register(gated_tool(raw_shell, manager=approval_manager))
    else:
        reg.register(raw_write)
        reg.register(raw_shell)

    return reg


__all__ = ["CODING_SYSTEM_PROMPT", "CodingAgent"]
