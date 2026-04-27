# CodingAgent — design

oxenClaw's port of openclaw's `pi-coding-agent` role: a `PiAgent`
specialisation that talks to the same WebSocket gateway but with a
curated tool bundle and a plan-first prompt aimed at file edits and
shell work. This document describes the goal, the parts that landed
in the current session as a "minimum-viable skeleton", and the
sub-tasks the next session should pick up.

---

## Goal

Provide a coding-focused agent the dashboard (and CLI) can spin up
without compromising the general-purpose `PiAgent` defaults. The
agent should:

1. Default to a **plan-first** workflow — emit a `<plan>` block
   before any state-changing tool call, then execute each step.
2. Carry **only** file-system + shell tools so the model's attention
   isn't diluted by weather / search / GitHub / skill-creator.
3. Run **destructive** tools (`write_file`, `shell`) under the
   existing `ApprovalManager` when one is injected, leaving
   read-only tools (`read_file`, `list_dir`, `search_files`)
   ungated.
4. Produce a structured summary at the end of every turn: files
   changed, commands run.

## Non-goals (this session)

- File-tree / current-file / diff-viewer panels in the dashboard.
- A dedicated "Code" tab in `app.js` (parking until the backend
  surface is settled).
- Sub-agent spawn / ACP-style coordination (separate ROADMAP item).
- Plan-event streaming as a first-class WS event channel — for now
  the plan lives inside the assistant message text.
- Patch-format application (`apply_patch` against unified diffs).
  `write_file` (full overwrite) is the v1 mechanism; `apply_patch`
  is on the next-session list.

---

## Architecture

```
                ┌────────────────────────────┐
                │  PiAgent (parent)          │
                │  - run loop, history,      │
                │    skills, memory, cache   │
                └──────────────┬─────────────┘
                               │ inheritance
                               ▼
                ┌────────────────────────────┐
                │  CodingAgent               │
                │  - CODING_SYSTEM_PROMPT    │
                │  - curated ToolRegistry    │
                │  - approval-gated writes   │
                └──────────────┬─────────────┘
                               │ uses
                               ▼
       ┌───────────────────────┴────────────────────────┐
       │           oxenclaw/tools_pkg/fs_tools.py        │
       │   read_file • write_file • list_dir •           │
       │   search_files • shell_run                      │
       └─────────────────────────────────────────────────┘
                               │ approval-gates
                               ▼
       ┌─────────────────────────────────────────────────┐
       │   oxenclaw/approvals.tool_wrap.gated_tool       │
       │   (active when an ApprovalManager is injected)  │
       └─────────────────────────────────────────────────┘

           Dashboard surface (deferred — next session):

       ┌────────────────────────────────────────────────┐
       │  "Code" tab (app.js)                           │
       │  - file tree pane (paths, click to read)       │
       │  - current-file viewer with read-only diff     │
       │  - plan progress bar derived from <plan> block │
       │  - approval-pending overlay for gated tools    │
       └────────────────────────────────────────────────┘
```

## Tool bundle

Source: `oxenclaw/tools_pkg/fs_tools.py` (created in this session).
Each tool is a `FunctionTool` with a Pydantic input model so the
LLM gets a strict JSON schema. CodingAgent registers the bundle in
`__init__`, optionally wrapping the write-side tools with
`gated_tool(...)` when an `ApprovalManager` is injected.

| Tool         | Inputs                                  | Side effects                                       | Gated? |
|--------------|------------------------------------------|----------------------------------------------------|--------|
| `read_file`  | `path: str`, `max_chars: int = 32_000`   | Read-only; returns text or "(not text)" sentinel.  | No |
| `list_dir`   | `path: str`, `glob: str \| None`         | Read-only; returns name + size + kind rows.        | No |
| `search_files` | `pattern: str`, `path: str`, `max: int` | Read-only; substring grep, returns line hits.      | No |
| `write_file` | `path: str`, `content: str`, `mkdirs: bool` | Creates or overwrites a file.                   | **Yes** when `ApprovalManager` is wired. |
| `shell_run`  | `command: str`, `cwd: str \| None`, `timeout_s: int` | Runs the command via the shared `shell_tool` sandbox. | **Yes** when `ApprovalManager` is wired. |

Read-only tools are intentionally never gated; gating them would
turn the agent's reconnaissance phase into an approval flood.

The `shell_run` tool delegates to the existing
`oxenclaw.security.shell_tool` so RLIMIT / bwrap sandboxing comes
along for free. This lets `CodingAgent` run `pytest` / `npm test` /
`make` with the same isolation as any other shell-using agent.

## System prompt

`CODING_SYSTEM_PROMPT` in `oxenclaw/agents/coding_agent.py` contains
the plan-first contract verbatim:

- Emit a `<plan>` block before making any changes.
- Read before write.
- Approval-gated tools may block; never retry a denied call.
- End-of-turn summary lists every file edited and every shell
  command run.

The prompt deliberately mentions which tools are approval-gated so
the model can preempt the gate by asking for user confirmation in
text first. We preserve PiAgent's skills / memory contributions
(skills.list and memory recall still feed the system prompt) so the
operator can still inject domain skills via clawhub.

## Approval flow

When the gateway boots with an `ApprovalManager`, `CodingAgent`
wraps `write_file` and `shell_run` with `gated_tool(...)`. The
gated wrapper intercepts each call, enqueues an
`ApprovalRequest`, and parks the tool execution until the operator
resolves it via `exec-approvals.resolve` (existing RPC). On
approve → original tool runs and returns the output. On deny → the
tool returns a "denied" `ToolExecutionResult.is_error=True` so the
LLM can surface the denial to the user.

Read-only tools never enqueue approvals — a coding session would
otherwise drown in ungated `read_file` confirmations during
exploration.

## Session schema

This session intentionally keeps the **same** `ConversationHistory`
schema as `PiAgent` — the dashboard's `chat.history` poll keeps
working unchanged. A future iteration may extend the schema with:

- `working_dir` — the project root the agent is editing under.
- `open_files` — the file paths the agent has read this turn.
- `plan` — the parsed `<plan>` block + step status.

These are deferred until a Code tab actually renders them; storing
without a consumer just creates schema drift.

## Factory wiring

`oxenclaw.agents.factory.build_agent` accepts a new keyword
`agent_type="coding"` (default `"chat"`):

```python
from oxenclaw.agents.factory import build_agent
agent = build_agent(
    agent_id="coding",
    provider="anthropic",
    model="claude-sonnet-4-6",
    agent_type="coding",
)
```

The CLI does not yet expose this — operators register a
`CodingAgent` programmatically (e.g. when defining their second
agent in `config.yaml`). A `--agent-type` CLI flag is on the
next-session list.

## Tests

`tests/test_coding_agent.py`:

- `test_factory_builds_coding_agent_when_requested` —
  `build_agent(agent_type="coding")` returns a `CodingAgent`.
- `test_coding_agent_registers_curated_tools` — the agent's tool
  registry exposes `read_file`, `write_file`, `list_dir`,
  `search_files`, `shell` and **does not** include the
  general-purpose `weather`, `web_fetch`, `github` etc.
- `test_coding_agent_uses_coding_system_prompt` —
  `agent._system_prompt` differs from PiAgent's default and
  mentions "plan" and "edit".

The existing PiAgent regression tests carry forward unchanged
because the inheritance is additive.

## What's shipped vs deferred

### Shipped this session

- `oxenclaw/agents/coding_agent.py` — `CodingAgent` class +
  curated registry + approval-aware wrapping.
- `oxenclaw/tools_pkg/fs_tools.py` — minimal `read_file`,
  `write_file`, `list_dir`, `search_files`, `shell_run` tools.
- `oxenclaw/agents/factory.py` — `agent_type="coding"` route.
- `tests/test_coding_agent.py` — three regression tests.
- This design document.

### Deferred to the next session (P2-F continued)

Ordered by user-visible impact:

1. **Dashboard "Code" tab** — file tree, current-file viewer,
   diff preview, plan progress bar (`docs/ROADMAP.md` P2-F.UI).
2. **`apply_patch` tool** — unified-diff application instead of
   full-file overwrites; reduces token spend on big files.
3. **Plan-event WS stream** — surface the parsed `<plan>`
   structurally so the dashboard can render a real progress bar
   instead of regex-extracting from the assistant text.
4. **`--agent-type` CLI flag** — let operators boot a coding agent
   from `oxenclaw gateway start` without touching `config.yaml`.
5. **Sub-agent spawn** — port openclaw's ACP-style child-agent
   pattern (separate ROADMAP item P2-J; the CodingAgent is a good
   first caller of it once it lands).

## Biggest open question for the next session

Whether the `<plan>` parsing should live in `CodingAgent` (text
sniffing) or be promoted to a **first-class structured tool**
(`plan(steps=[…])`) the LLM is forced through. Structured plans
are easier to render (no regex / fragile XML parsing) but require
prompt engineering to keep the model from emitting freeform plans
out of habit. Pick this before the dashboard work — the dashboard
contract follows the parser shape.
