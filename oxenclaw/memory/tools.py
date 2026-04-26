"""Memory tools the agent can invoke during a turn.

Three tools:
  - `memory_save`   — append text to inbox.md then re-index
  - `memory_search` — vector-search the corpus, return chunks + citations
  - `memory_get`    — read a slice of a file by relative path
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.memory.retriever import MemoryRetriever


class _SaveArgs(BaseModel):
    model_config = {"extra": "forbid"}
    text: str = Field(..., description="The fact to remember (one or two sentences).")
    tags: list[str] = Field(
        default_factory=list,
        description="Optional categorical labels (e.g. 'preference', 'fact').",
    )


class _SearchArgs(BaseModel):
    model_config = {"extra": "forbid"}
    query: str = Field(..., description="What to recall — semantic phrase or question.")
    k: int = Field(5, description="How many results to return.", ge=1, le=20)
    source: str | None = Field(
        None, description="Optional source filter (e.g. 'memory', 'sessions')."
    )


class _GetArgs(BaseModel):
    model_config = {"extra": "forbid"}
    path: str = Field(..., description="Relative path inside the memory corpus.")
    from_line: int = Field(1, description="1-indexed start line.", ge=1)
    lines: int = Field(120, description="Number of lines to read.", ge=1, le=500)


def memory_save_tool(retriever: MemoryRetriever) -> Tool:
    async def _handler(args: _SaveArgs) -> str:
        report = await retriever.save(args.text, tags=args.tags or None)
        return (
            f"saved to {retriever.inbox_path.name}; reindexed "
            f"{report.added + report.changed} file(s), "
            f"{report.chunks_embedded} chunk(s)"
        )

    return FunctionTool(
        name="memory_save",
        description=(
            "Persist a fact about the user or task to long-term memory. "
            "Appends to the inbox markdown file. Tags help organise."
        ),
        input_model=_SaveArgs,
        handler=_handler,
    )


def memory_search_tool(retriever: MemoryRetriever) -> Tool:
    async def _handler(args: _SearchArgs) -> str:
        hits = await retriever.search(args.query, k=args.k, source=args.source)
        if not hits:
            return "(no memories matched)"
        lines: list[str] = []
        for h in hits:
            preview = h.chunk.text.strip().splitlines()[0][:160]
            lines.append(f"- ({h.score:.2f}) {h.citation}  {preview}")
        return "\n".join(lines)

    return FunctionTool(
        name="memory_search",
        description=(
            "Recall relevant chunks from the memory corpus by semantic "
            "similarity. Returns citations of the form path:start-end."
        ),
        input_model=_SearchArgs,
        handler=_handler,
    )


def memory_get_tool(retriever: MemoryRetriever) -> Tool:
    async def _handler(args: _GetArgs) -> str:
        result = retriever.get(args.path, from_line=args.from_line, lines=args.lines)
        body = result.text or "(empty)"
        if result.truncated and result.next_from is not None:
            body += f"\n\n[More available: from={result.next_from}]"
        return body

    return FunctionTool(
        name="memory_get",
        description=(
            "Read a line range from a memory file by path. Use after "
            "`memory_search` to read the full context behind a citation."
        ),
        input_model=_GetArgs,
        handler=_handler,
    )
