# Authoring Skills & Tools

How to extend sampyClaw with your own task-specific automation. **This is
the recommended path for users who want to teach the agent new tricks.**
If you instead want to import a tool that already exists as an MCP server,
read the "Importing existing MCP servers" section near the bottom — that
path is supported (M1, shipped 2026-04-25).

## TL;DR — write two files

```
~/.sampyclaw/skills/<your-slug>/
├── SKILL.md              # tells the model the tool exists + when to use it
└── <your_slug>.py        # the actual Python tool
```

Then register the tool with the agent's `ToolRegistry` (one line at
startup). That's the whole loop.

---

## Concept map

sampyClaw separates **what the model knows about** (a *skill*) from
**what the model can call** (a *tool*). Most useful capabilities pair
the two:

| Concept | What it is | Where it lives | How the agent sees it |
|---|---|---|---|
| **Skill** | A markdown file with frontmatter — name, description, body explaining usage | `~/.sampyclaw/skills/<slug>/SKILL.md` | Auto-discovered, rendered into the system prompt as an `<available_skills>` block |
| **Tool** | A Python callable with a Pydantic input schema | Anywhere importable (typically the skill dir) | Registered explicitly on `ToolRegistry`; rendered as Anthropic/OpenAI `tools` param |

You can ship a skill with no tool (pure prose guidance for the model),
or a tool with no skill (the agent calls it directly). The combo is the
common case.

---

## Step 1 — Scaffold

The fastest way is the bundled `skill_creator` tool (from inside an
agent session, or via `sampyclaw message send` against a running gateway):

```text
agent: please create a new skill called "ticket lookup" that takes a
       Linear ticket id and returns the description.
       (write_tool_stub: true)
```

The agent calls `skill_creator` which writes:

```
~/.sampyclaw/skills/ticket-lookup/SKILL.md
~/.sampyclaw/skills/ticket-lookup/ticket_lookup.py    # stub tool
```

Or scaffold by hand — the file shapes are documented below.

### SKILL.md shape

```markdown
---
name: ticket-lookup
description: "Look up a Linear ticket by id and return title + description."
homepage: https://example.com
openclaw:
  emoji: "🎫"
  requires:
    anyBins: [linear, lr]      # optional — only if your tool shells out
  env_overrides:                # optional — for clawhub-installed skills
    LINEAR_API_KEY: "$LINEAR_API_KEY"
---

# ticket-lookup

Use this tool whenever the user asks about a Linear ticket. Input is a
ticket id like `ENG-1234`. Returns a markdown block with title and body.

## Examples

- "what's ENG-1234 about?" → call `ticket_lookup(ticket_id="ENG-1234")`
- "summarise PROJ-99" → call `ticket_lookup(ticket_id="PROJ-99")`
```

The body is read by the model when activating the skill. **Concrete
examples beat abstract descriptions** — they steer tool selection more
reliably than adjectives.

Frontmatter parsing: `sampyclaw/clawhub/frontmatter.py`
(`SkillManifest` + `parse_skill_text`).

### Tool shape

```python
# ~/.sampyclaw/skills/ticket-lookup/ticket_lookup.py
from __future__ import annotations

from pydantic import BaseModel, Field

from sampyclaw.agents.tools import FunctionTool, Tool


class _Args(BaseModel):
    ticket_id: str = Field(..., description="Linear ticket id, e.g. ENG-1234")


def ticket_lookup_tool() -> Tool:
    async def _h(args: _Args) -> str:
        # ... real implementation ...
        return f"# {args.ticket_id}\n\n(body here)"

    return FunctionTool(
        name="ticket_lookup",
        description="Fetch a Linear ticket and return its title + body as markdown.",
        input_model=_Args,
        handler=_h,
    )
```

Anything that satisfies `sampyclaw.agents.tools.Tool` (Protocol with
`name`, `description`, `input_schema`, `async execute`) works —
`FunctionTool` is the convenient base.

---

## Step 2 — Register the tool with the agent

**Skills are auto-discovered. Tools are not.** The loader walks
`~/.sampyclaw/skills/` for `SKILL.md` files, but Python modules in those
directories are not auto-imported (this is a deliberate security choice
— see [`SECURITY.md`](./SECURITY.md)).

You wire a tool in by passing a `ToolRegistry` to `build_agent`:

```python
from sampyclaw.agents.factory import build_agent
from sampyclaw.agents.tools import ToolRegistry
from sampyclaw.agents.builtin_tools import default_tools

# import your tool
import sys
sys.path.insert(0, "/home/me/.sampyclaw/skills/ticket-lookup")
from ticket_lookup import ticket_lookup_tool

tools = ToolRegistry()
tools.register_all(default_tools())
tools.register(ticket_lookup_tool())

agent = build_agent(
    agent_id="default",
    provider="pi",
    tools=tools,
)
```

If you're embedding sampyClaw in your own Python code, this is the
canonical wiring. If you're running `sampyclaw gateway start` and want
your tools loaded automatically, the cleanest path is to ship them as
a **plugin package** with an entry point — see "Distributing as a
plugin" below.

---

## Step 3 — Verify

```bash
# the gateway shows what got loaded
sampyclaw skills list

# direct sanity-check of a skill's frontmatter
sampyclaw skills show ticket-lookup

# end-to-end: send a message that should trigger the tool
sampyclaw message send --agent default "summarise ENG-1234"
```

If the tool didn't fire: check the agent's transcript for which tools
were exposed (`agent.tools.names()`), and confirm the model saw the
SKILL.md (it'll be in the system-prompt's `<available_skills>` block).

---

## Layered features (opt-in, all free)

When you wrap your tool with sampyClaw infrastructure, you inherit
production-grade behavior at zero authoring cost.

### Approval gating

```python
from sampyclaw.approvals.tool_wrap import gated_tool
from sampyclaw.approvals.manager import ApprovalManager

manager = ApprovalManager(state_path=..., approver_token="...")
tools.register(gated_tool(ticket_lookup_tool(), manager=manager))
```

Now every call requires a human approval (resolved via the
`exec-approvals.*` gateway RPC, dashboard UI, or `ApprovalManager.resolve()`).
The tool returns `"tool call denied: ..."` / `"tool call timed out"` /
`"tool call cancelled"` distinct strings so the model can decide whether
to retry. See `sampyclaw/approvals/tool_wrap.py`.

### Outbound network guards (SSRF / DNS pinning / audit)

If your tool makes HTTP calls, use `guarded_session` instead of `aiohttp.ClientSession`:

```python
from sampyclaw.security.net.guarded_fetch import guarded_session
from sampyclaw.security.net.policy import policy_from_env

async with guarded_session(policy_from_env()) as session:
    async with session.get("https://api.linear.app/graphql") as resp:
        ...
```

You inherit:

- IPv4/IPv6 special-range blocking (loopback, RFC1918, link-local, ULA, multicast, …)
- Loose-IPv4 literal refusal (`0x7f.0.0.1`, `127.1`, etc.)
- Per-redirect re-validation
- DNS pinning (resolved IP cached + checked against policy)
- Optional audit (env-gated, off by default — `SAMPYCLAW_AUDIT_OUTBOUND=1`)

See `sampyclaw/security/net/`.

### Sandbox / isolation

If your tool runs untrusted code (or you just want belt-and-suspenders
isolation), use `IsolatedFunctionTool` instead of `FunctionTool`:

```python
from sampyclaw.agents.isolation import IsolatedFunctionTool, IsolationPolicy

return IsolatedFunctionTool(
    name="ticket_lookup",
    description="...",
    input_model=_Args,
    handler=_h,
    policy=IsolationPolicy(
        network=False,            # default-deny
        filesystem="readonly",
        max_processes=64,         # RLIMIT_NPROC
        max_stdin_bytes=4 << 20,  # 4 MiB
        tmpfs_size_mb=64,
    ),
)
```

Backends: `subprocess` (RLIMIT-only, fail-closed when policy demands more)
and `bwrap` (mount + namespace isolation). See
`sampyclaw/agents/isolation.py`.

### Multimodal (image input)

If your channel plugin populates `InboundEnvelope.media` with
`MediaItem(kind="photo", source=...)` items, the agent receives them as
image blocks **automatically** — no per-tool wiring required. The
runtime handles:

- **Capability gating** — `multimodal.model_supports_images(model_id)`
  consults the pi catalog (`gemma4:latest` / `claude-sonnet-4-6` /
  `gpt-4o` / `gemini-1.5-pro` / `llava` / `llama3.2-vision` / …) plus a
  heuristic substring match for non-cataloged tags.
- **Normalization** — `multimodal.normalize_media_item()` accepts
  `data:` URIs and `http(s)://` URLs (the latter goes through the
  SSRF-guarded `guarded_session` so DNS pinning + private-range blocking
  apply). 10 MiB cap, MIME sniffing of JPEG/PNG/GIF/WebP magic bytes.
- **Provider serialization** — each agent puts the image into the shape
  its API expects: Anthropic `{type:"image", source:{type:"base64",...}}`,
  OpenAI/Ollama `{type:"image_url", image_url:{url:"data:..."}}`,
  Google `{inline_data:{mime_type, data}}`, pi `ImageContent`.
- **Graceful degradation** — when the active model is text-only, the
  runtime drops the image and prepends a `(N image(s) dropped: model X
  does not support image input)` line to the user message so the LLM
  knows context was lost rather than silently misunderstanding.

If you're authoring a **tool** that wants to *consume* images (OCR
helper, image-to-caption tool, …), accept the `data:` URI as a Pydantic
field and reuse `multimodal.normalize_media_item` to validate.

If you're authoring a **channel plugin** that delivers images, populate
`MediaItem.source` with either:

- a `data:image/<jpg|png|gif|webp>;base64,<payload>` URI (preferred —
  no extra round-trip), or
- an `http(s)://` URL the gateway can fetch with NetPolicy applied.

### Memory recall

Inject the agent's memory retriever and your tool can pull relevant
prior context:

```python
from sampyclaw.memory.retriever import MemoryRetriever

def ticket_lookup_tool(memory: MemoryRetriever) -> Tool:
    async def _h(args: _Args) -> str:
        prior = await memory.search(args.ticket_id, k=3)
        ...
```

### Cron / scheduled execution

If your skill is "run X every morning", register it as a cron job
(`sampyclaw cron add`) — the same tool implementation is reused.

---

## Distributing as a plugin

For shareable / per-team tools, package as a normal Python distribution
with an entry point. See `sampyclaw/plugin_sdk/` and the existing
`extensions/telegram/` plugin as the canonical reference.

```toml
# pyproject.toml
[project.entry-points."sampyclaw.channels"]
my_channel = "my_pkg.channel:plugin"

[project.entry-points."sampyclaw.skills"]   # planned hook for tool discovery
my_skill = "my_pkg.skill:contribute_tools"
```

`pip install -e .` and `discover_plugins()` picks it up on next gateway
restart. Verified in the integration capstone — zero channel-specific
code in the CLI.

---

## When NOT to write a skill+tool — write an MCP server instead

| Situation | Pick |
|---|---|
| sampyClaw-only, you write Python | **skill+tool** |
| You want the same tool to work in Claude Desktop / Cursor / Codex too | MCP server (server-side phase planned, see SUBSYSTEM_MAP.md) |
| You write Go/Rust/TS, not Python | MCP server |
| Someone already shipped an MCP server you want to use | **MCP client** — see "Importing existing MCP servers" below |
| You need approval gating + SSRF guards + sandbox + memory | **skill+tool** (MCP doesn't get these for free) |
| Quick personal automation | **skill+tool** |

In short: **MCP optimises for cross-client portability; skill+tool
optimises for sampyClaw's policy stack.** Pick the one that matches your
distribution goal.

---

## Importing existing MCP servers (escape hatch)

If a third party already wrote an MCP server you want to use, you don't
have to reimplement it as a sampyClaw skill. sampyClaw includes an MCP
**client** (M1 phase, `sampyclaw/pi/mcp/`) that connects to MCP servers
and surfaces their tools as if they were native sampyClaw tools.

### Configure

Drop a config at `~/.sampyclaw/mcp.json` using the standard shape (the
same shape Claude Desktop and `mcp` CLI use, so configs are portable):

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    },
    "remote": {
      "url": "https://mcp.example.com/sse",
      "headers": { "Authorization": "Bearer ${MY_TOKEN}" }
    }
  }
}
```

`$VAR` and `${VAR}` references are expanded against `os.environ` at load
time. Unknown vars are left as the literal reference (so missing tokens
are visible, not silently empty).

### Wire into the agent

```python
from sampyclaw.agents.factory import build_agent, load_mcp_tools

mcp_tools, pool = await load_mcp_tools()   # reads ~/.sampyclaw/mcp.json
agent = build_agent(
    agent_id="default",
    provider="pi",
    mcp_tools=mcp_tools,
)
# ... agent run ...
if pool is not None:
    await pool.close()                       # tear down subprocesses / SSE
```

What you get for free:

- **stdio transport** — the MCP server runs as a subprocess. Inherited
  env strips loader-affecting keys (`LD_PRELOAD`, `PATH`,
  `PYTHONSTARTUP`, …) before spawn. User-supplied dangerous env keys are
  also dropped.
- **HTTP+SSE transport** — outbound calls go through
  `security/net/guarded_session`, so SSRF / DNS pinning / scheme + port
  guards apply. URLs are pre-flighted.
- **Failure isolation** — one broken server doesn't fail the pool. Its
  tools simply don't appear; the reason is in `pool.failures`.
- **Name de-collision** — MCP tool names are mangled to
  `<safe_server>__<safe_tool>`, capped at 64 chars, deduped against
  reserved names (your native tools).
- **Result rendering** — MCP `CallToolResult` content blocks
  (text/image/resource) flatten to a single string the model can read,
  with `isError` markers preserved.

### Limits / not yet supported

- Server-side: **exposing sampyClaw's own tools as an MCP server** is a
  separate phase (M2) and is not yet implemented.
- Authorization beyond static headers (OAuth flows, mutual TLS) is
  out of scope — set up a token-issuing proxy if you need it.
- Streamable-HTTP is parsed and accepted, but treated as SSE at the wire
  level (the common case). Pure-POST streaming responses haven't been
  exercised in production.

---

## Live references

These bundled skills are the working examples — read their source
directly, they're 50–200 LOC each:

| Skill | What to learn from it |
|---|---|
| [`summarize`](../sampyclaw/skills/summarize/) + [`tools_pkg/summarize.py`](../sampyclaw/tools_pkg/summarize.py) | Pure-LLM sub-call (no external deps) |
| [`weather`](../sampyclaw/skills/weather/) + [`tools_pkg/weather.py`](../sampyclaw/tools_pkg/weather.py) | HTTP fetch with SSRF guard, multi-provider fallback |
| [`github`](../sampyclaw/skills/github/) + [`tools_pkg/github.py`](../sampyclaw/tools_pkg/github.py) | Shelling out to a CLI (`gh`) with verb allow-list |
| [`session_logs`](../sampyclaw/skills/session_logs/) + [`tools_pkg/session_logs.py`](../sampyclaw/tools_pkg/session_logs.py) | Reading sampyClaw's own internal state (meta-tool pattern) |
| [`healthcheck`](../sampyclaw/skills/healthcheck/) + [`tools_pkg/healthcheck.py`](../sampyclaw/tools_pkg/healthcheck.py) | Aggregating multiple subsystem probes |
| [`skill_creator`](../sampyclaw/skills/skill_creator/) + [`tools_pkg/skill_creator.py`](../sampyclaw/tools_pkg/skill_creator.py) | Writing files into the skills dir + frontmatter validation |

---

## Common pitfalls

1. **Forgetting to register the tool.** SKILL.md alone won't make the
   model able to call anything — the model will hallucinate a tool call
   that fails. Always pair `SKILL.md` with `tools.register(...)`.
2. **Slug mismatch.** SKILL.md `name:` field, directory name, and tool
   `name=` should align (e.g. `ticket-lookup` / `ticket-lookup/` /
   `"ticket_lookup"`). Hyphens in dir/skill name, underscores in tool
   name (so the model gets a valid identifier).
3. **No examples in the skill body.** The model picks tools based on
   prose pattern-matching. Two concrete examples beat a paragraph of
   description.
4. **Direct `aiohttp` instead of `guarded_session`.** Skips SSRF + DNS
   pinning. Always use the guarded variant for outbound HTTP unless
   you have a deliberate reason.
5. **Catching exceptions too aggressively.** Let raised exceptions
   surface — the agent harness formats them as tool errors that the
   model can self-correct on. A tool that swallows everything and
   returns `"ok"` makes the model think it succeeded.

---

## Decision tree (quick)

```
Q1. Is the tool sampyClaw-only?
    YES → skill+tool
    NO  → Q2

Q2. Will the same tool be invoked by Claude Desktop / Cursor / etc?
    YES → MCP server (after M2 phase)
    NO  → skill+tool

Q3. Are you using an existing MCP server someone else wrote?
    YES → wait for M1 (MCP client phase, planned)
    NO  → skill+tool
```

When in doubt: **skill+tool**. Migrate to MCP later if cross-client
demand emerges; the migration cost is low because the tool body is
already isolated.
