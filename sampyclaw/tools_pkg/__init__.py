"""In-tree tools beyond the trivial echo/get_time set.

Modules:
- `web`         — `web_fetch` + `web_search` (SSRF-guarded)
- `subagent`    — `subagents` tool (spawn child PiAgent)
- `cron_tool`   — `cron` tool (LLM-callable cron registration)
- `message_tool`— `message` tool (LLM-callable cross-channel send)
- `coding`      — `coding_agent` tool (delegate to claude/codex/pi/opencode)

These plug into the existing `agents.tools.ToolRegistry`. The PiAgent
default tool set is curated in `tools_pkg.bundle.default_in_tree_tools`.
"""
