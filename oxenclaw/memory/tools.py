"""Memory tools the agent can invoke during a turn.

Three tools:
  - `memory_save`   — append text to inbox.md then re-index
  - `memory_search` — vector-search the corpus, return chunks + citations
  - `memory_get`    — read a slice of a file by relative path
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.memory.privacy import scan_memory_threats
from oxenclaw.memory.retriever import MemoryRetriever
from oxenclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("memory.tools")


# LLM tool-calling drift: GPT-4-class models often emit aliases like
# `content` / `body` / `note` for `text`, and `key` / `category` /
# `label` / `tag` (singular) for `tags`. Strict `extra=forbid` rejects
# the call outright; we fold these aliases in with a model-level
# `before` validator instead. Matches openclaw's permissive intake.
_SAVE_TEXT_ALIASES = ("content", "body", "note", "fact", "value", "data")
_SAVE_TAG_ALIASES = ("tag", "key", "category", "label", "kind")
# Known canonical fields. Anything outside these (and the aliases
# above) gets dropped by the `before` validator so an LLM sprinkling
# `source` / `metadata` / `confidence` etc. on the call doesn't break
# strict validation.
_SAVE_KNOWN_FIELDS = {"text", "tags"}
_SEARCH_KNOWN_FIELDS = {"query", "k", "source"}


class _SaveArgs(BaseModel):
    model_config = {"extra": "forbid"}
    text: str = Field(..., description="The fact to remember (one or two sentences).")
    tags: list[str] = Field(
        default_factory=list,
        description="Optional categorical labels (e.g. 'preference', 'fact').",
    )

    @model_validator(mode="before")
    @classmethod
    def _absorb_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        # Map text-aliases. First non-empty value wins; the original
        # alias key is then dropped so `extra=forbid` doesn't trip.
        if "text" not in out or not out.get("text"):
            for alias in _SAVE_TEXT_ALIASES:
                if out.get(alias):
                    out["text"] = out[alias]
                    break
        for alias in _SAVE_TEXT_ALIASES:
            out.pop(alias, None)
        # Map tag-aliases. Single-value aliases lift into the list.
        existing = list(out.get("tags") or [])
        for alias in _SAVE_TAG_ALIASES:
            if out.get(alias):
                v = out[alias]
                if isinstance(v, list):
                    existing.extend(str(x) for x in v if x)
                else:
                    existing.append(str(v))
            out.pop(alias, None)
        # Dedup while preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for t in existing:
            if t not in seen:
                seen.add(t)
                deduped.append(t)
        if deduped:
            out["tags"] = deduped
        # Drop any remaining unknown fields silently. LLMs sprinkle
        # `source`, `metadata`, `confidence`, `timestamp`, `tool` etc.
        # onto memory_save calls — none of those break strict
        # validation if we strip them here, because every alias we
        # actually care about was popped above.
        return {k: v for k, v in out.items() if k in _SAVE_KNOWN_FIELDS}


_SEARCH_QUERY_ALIASES = ("question", "q", "prompt", "text", "topic")


class _SearchArgs(BaseModel):
    model_config = {"extra": "forbid"}
    query: str = Field(..., description="What to recall — semantic phrase or question.")
    k: int = Field(5, description="How many results to return.", ge=1, le=20)
    source: str | None = Field(
        None, description="Optional source filter (e.g. 'memory', 'sessions')."
    )

    @model_validator(mode="before")
    @classmethod
    def _absorb_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if "query" not in out or not out.get("query"):
            for alias in _SEARCH_QUERY_ALIASES:
                if out.get(alias):
                    out["query"] = out[alias]
                    break
        for alias in _SEARCH_QUERY_ALIASES:
            out.pop(alias, None)
        # `limit` is a common LLM emit for k.
        if "k" not in out and out.get("limit"):
            out["k"] = out["limit"]
        out.pop("limit", None)
        # Drop unknown keys silently (same rationale as _SaveArgs).
        return {k: v for k, v in out.items() if k in _SEARCH_KNOWN_FIELDS}


class _GetArgs(BaseModel):
    model_config = {"extra": "forbid"}
    path: str = Field(..., description="Relative path inside the memory corpus.")
    from_line: int = Field(1, description="1-indexed start line.", ge=1)
    lines: int = Field(120, description="Number of lines to read.", ge=1, le=500)


def memory_save_tool(retriever: MemoryRetriever) -> Tool:
    async def _handler(args: _SaveArgs) -> str:
        # Operator visibility: log the saved fact + tags so a
        # `tail -f gateway.log` shows whether memory_save actually
        # fired (vs. the model claiming it did but never calling).
        # 200-char preview keeps long pastes from flooding the log.
        preview = args.text.strip().replace("\n", " ")[:200]
        logger.info(
            "memory_save: tags=%s text=%r",
            args.tags or [],
            preview,
        )
        # Threat scan — memory entries land in the next session's
        # system prompt, so a stored prompt-injection survives across
        # sessions until manually scrubbed. Refuse to persist anything
        # that matches a known injection / role-hijack / exfil pattern
        # and report the kinds back to the model so it can paraphrase.
        threats = scan_memory_threats(args.text)
        if threats:
            kinds = sorted({t.kind for t in threats})
            logger.warning(
                "memory_save: refused write — threats=%s preview=%r",
                kinds,
                preview,
            )
            return (
                "memory_save refused: text matches a memory-write threat "
                f"pattern ({', '.join(kinds)}). Persistent memory feeds "
                "future system prompts, so injection-shaped content cannot "
                "be stored. Rephrase as a plain factual statement (no "
                "instructions, no shell commands, no role-changing meta-"
                "directives) and try again."
            )
        report = await retriever.save(args.text, tags=args.tags or None)
        if report.dedup_replaced:
            return (
                f"updated existing entry in {retriever.inbox_path.name} "
                "(near-duplicate detected; tags merged, timestamp refreshed)"
            )
        return (
            f"saved to {retriever.inbox_path.name}; reindexed "
            f"{report.added + report.changed} file(s), "
            f"{report.chunks_embedded} chunk(s)"
        )

    return FunctionTool(
        name="memory_save",
        description=(
            "Persist a fact about the user or task to long-term memory. "
            "`text` MUST be a complete natural-language sentence, not a "
            '`key:value` line — "User lives in Suwon, South Korea." '
            'beats "user_location:Suwon" because the embedding store '
            "scores conversational queries against it. When the user "
            "wrote in a non-English language, include BOTH their "
            "phrasing and an English paraphrase in `text` so cross-"
            "language recall (e.g. Korean question → fact saved in "
            "English) hits the chunk. `tags` is an optional list of "
            "short labels."
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
