"""chat.* RPCs beyond `chat.send` — read/clear per-session history + session enumeration."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from oxenclaw.agents.history import ConversationHistory
from oxenclaw.agents.registry import AgentRegistry
from oxenclaw.config.paths import OxenclawPaths, default_paths
from oxenclaw.gateway.router import Router


class _HistoryParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str
    session_key: str
    limit: int | None = None


class _ClearParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str
    session_key: str


class _ListSessionsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str


class _DebugPromptParams(BaseModel):
    """`chat.debug_prompt` — return the assembled system prompt that a
    given agent would build for a given query. Operator-only diagnostic
    that lets you see exactly what the model is being told (recalled
    memories, skill block, base playbook) without running a turn."""

    model_config = ConfigDict(extra="forbid")
    agent_id: str
    query: str = Field(..., min_length=1)


def register_chat_methods(
    router: Router,
    *,
    paths: OxenclawPaths | None = None,
    agents: AgentRegistry | None = None,
) -> None:
    resolved = paths or default_paths()

    @router.method("chat.history", _HistoryParams)
    async def _history(p: _HistoryParams) -> dict:  # type: ignore[type-arg]
        hist = ConversationHistory(resolved.session_file(p.agent_id, p.session_key))
        messages = hist.messages()
        if p.limit is not None:
            messages = messages[-p.limit :]
        return {"messages": messages, "total": len(hist)}

    @router.method("chat.clear", _ClearParams)
    async def _clear(p: _ClearParams) -> dict:  # type: ignore[type-arg]
        path = resolved.session_file(p.agent_id, p.session_key)
        existed = path.exists()
        if existed:
            path.unlink()
        return {"cleared": existed}

    @router.method("chat.list_sessions", _ListSessionsParams)
    async def _list_sessions(p: _ListSessionsParams) -> dict:  # type: ignore[type-arg]
        sessions_dir = resolved.agent_dir(p.agent_id) / "sessions"
        if not sessions_dir.exists():
            return {"sessions": []}
        rows = []
        for f in sorted(sessions_dir.glob("*.json")):
            try:
                stat = f.stat()
            except OSError:
                continue
            rows.append(
                {
                    "session_key": f.stem,
                    "size": stat.st_size,
                    "modified_at": stat.st_mtime,
                }
            )
        rows.sort(key=lambda r: r["modified_at"], reverse=True)
        return {"sessions": rows}

    if agents is not None:
        @router.method("chat.debug_prompt", _DebugPromptParams)
        async def _debug_prompt(p: _DebugPromptParams) -> dict:  # type: ignore[type-arg]
            agent = agents.get(p.agent_id)
            if agent is None:
                return {"ok": False, "error": f"agent {p.agent_id!r} not registered"}
            debug = getattr(agent, "debug_assemble", None)
            if debug is None:
                return {
                    "ok": False,
                    "error": (
                        f"agent {p.agent_id!r} ({type(agent).__name__}) does not "
                        "expose debug_assemble — only PiAgent supports this RPC"
                    ),
                }
            try:
                payload = await debug(p.query)
            except Exception as exc:
                return {"ok": False, "error": f"debug_assemble failed: {exc}"}
            return {"ok": True, **payload}
