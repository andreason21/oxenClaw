"""session_logs tool — let the agent search/read its own transcripts.

Mirrors openclaw `skills/session-logs`. Reads through a `SessionManager`
so it works with any backend (in-memory, sqlite, etc).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.pi.session import SessionManager
from oxenclaw.tools_pkg._arg_aliases import fold_aliases


def _block_text(block) -> str:  # type: ignore[no-untyped-def]
    if hasattr(block, "text"):
        return block.text
    if hasattr(block, "thinking"):
        return f"[thinking] {block.thinking[:200]}"
    if hasattr(block, "name"):
        return f"[tool_use {block.name}]"
    return str(block)[:200]


def _msg_preview(msg, *, max_chars: int = 240) -> str:  # type: ignore[no-untyped-def]
    role = msg.role
    content = getattr(msg, "content", None)
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(_block_text(b) for b in content)
    elif role == "tool_result":
        text = " ".join(
            r.content if isinstance(r.content, str) else "[blocks]"
            for r in getattr(msg, "results", [])
        )
    return f"{role}: {text[:max_chars]}"


class _LogsArgs(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _absorb(cls, data: Any) -> Any:
        return fold_aliases(
            data,
            {
                "action": ("op", "operation", "verb", "command"),
                "query": ("q", "search", "text", "pattern", "needle"),
                "agent_id": ("agent", "agentId"),
                "session_id": ("session", "sessionId"),
            },
        )

    action: Literal["list", "view", "grep"] = Field(...)
    agent_id: str | None = Field(None, description="Filter to one agent.")
    session_id: str | None = Field(None, description="Required for action=view.")
    last_n: int = Field(10, description="Last N turns for view.", gt=0)
    query: str | None = Field(None, description="Substring for action=grep.")
    limit: int = Field(20, description="Cap on rows returned.", gt=0)


def session_logs_tool(sessions: SessionManager) -> Tool:
    async def _h(args: _LogsArgs) -> str:
        if args.action == "list":
            rows = await sessions.list(agent_id=args.agent_id)
            rows = rows[: args.limit]
            if not rows:
                return "(no sessions)"
            return "\n".join(
                f"{r.id[:8]}  msgs={r.message_count:<4}  agent={r.agent_id}  "
                f"{r.title or '(untitled)'}"
                for r in rows
            )

        if args.action == "view":
            if not args.session_id:
                return "session_logs error: session_id required for action=view"
            s = await sessions.get(args.session_id)
            if s is None:
                return f"session_logs: no session {args.session_id!r}"
            tail = s.messages[-args.last_n :]
            lines = [f"session {s.id[:8]} ({len(s.messages)} messages):"]
            base_idx = len(s.messages) - len(tail)
            for off, m in enumerate(tail):
                lines.append(f"[{base_idx + off:>4}] {_msg_preview(m)}")
            return "\n".join(lines)

        if args.action == "grep":
            if not args.query:
                return "session_logs error: query required for action=grep"
            needle = args.query.lower()
            rows = await sessions.list(agent_id=args.agent_id)
            matches: list[str] = []
            for entry in rows:
                if len(matches) >= args.limit:
                    break
                s = await sessions.get(entry.id)
                if s is None:
                    continue
                for i, m in enumerate(s.messages):
                    preview = _msg_preview(m, max_chars=120).lower()
                    if needle in preview:
                        matches.append(f"{entry.id[:8]}#{i}  {_msg_preview(m, max_chars=200)}")
                        if len(matches) >= args.limit:
                            break
            if not matches:
                return f"no matches for {args.query!r}"
            return "\n".join(matches)

        return f"session_logs error: unknown action {args.action!r}"

    return FunctionTool(
        name="session_logs",
        description=(
            "Inspect agent session transcripts. action=list shows recent "
            "sessions; action=view session_id shows the last N turns; "
            "action=grep query searches across sessions."
        ),
        input_model=_LogsArgs,
        handler=_h,
    )


__all__ = ["session_logs_tool"]
