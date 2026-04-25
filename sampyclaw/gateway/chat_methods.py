"""chat.* RPCs beyond `chat.send` — read/clear per-session history + session enumeration."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from sampyclaw.agents.history import ConversationHistory
from sampyclaw.config.paths import SampyclawPaths, default_paths
from sampyclaw.gateway.router import Router


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


def register_chat_methods(router: Router, *, paths: SampyclawPaths | None = None) -> None:
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
