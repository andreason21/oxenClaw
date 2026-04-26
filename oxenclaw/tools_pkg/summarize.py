"""summarize tool — sub-LLM call that compresses text.

Mirrors openclaw `skills/summarize`. Takes `input_text` + `length` and
runs one isolated sub-turn through the same pi run loop the parent uses.
The caller must supply a `Model` + `Api` (via SubagentConfig-style
plumbing) so the tool stays provider-agnostic — operators wire it up
once at agent construction time.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.pi import (
    AssistantMessage,
    AuthStorage,
    Model,
    TextContent,
    UserMessage,
)
from oxenclaw.pi.auth import resolve_api
from oxenclaw.pi.run import RuntimeConfig, run_agent_turn

_LENGTH_INSTR = {
    "short": "Reply in 1-2 sentences.",
    "medium": "Reply in a single paragraph (3-5 sentences).",
    "long": "Reply in 2-3 short paragraphs covering all key points.",
    "bullets": "Reply as a bulleted list of 3-7 concise bullets.",
}


class _SummariseArgs(BaseModel):
    input_text: str = Field(..., description="The text to summarise.")
    length: Literal["short", "medium", "long", "bullets"] = Field(
        "medium", description="Target summary length / format."
    )
    focus: str | None = Field(None, description="Optional aspect to emphasise (e.g. 'risks only').")


def summarize_tool(
    *,
    model: Model,
    auth: AuthStorage,
    runtime: RuntimeConfig | None = None,
) -> Tool:
    """Build the summarize tool bound to a (model, auth) pair."""
    cfg = runtime or RuntimeConfig(temperature=0.2)

    async def _h(args: _SummariseArgs) -> str:
        instr = _LENGTH_INSTR[args.length]
        if args.focus:
            instr = f"{instr} Focus on: {args.focus}."
        prompt = f"Summarise the following text. {instr}\n\n---\n{args.input_text}\n---"
        api = await resolve_api(model, auth)
        result = await run_agent_turn(
            model=model,
            api=api,
            system="You write concise, accurate summaries.",
            history=[UserMessage(content=prompt)],
            tools=[],
            config=cfg,
        )
        if not isinstance(result.final_message, AssistantMessage):
            return "(no summary produced)"
        text = "\n".join(
            b.text for b in result.final_message.content if isinstance(b, TextContent)
        ).strip()
        return text or "(empty summary)"

    return FunctionTool(
        name="summarize",
        description=(
            "Summarise input_text at a target length (short / medium / long / "
            "bullets). Optional focus aspect."
        ),
        input_model=_SummariseArgs,
        handler=_h,
    )


__all__ = ["summarize_tool"]
