"""sessions_yield — voluntary cooperative-yield tool.

Mirrors openclaw `sessions_yield` (the `onYield` callback path in
`runEmbeddedAttempt`). When a long-running sub-agent decides to wait
for an external trigger (cron, user reply, sibling-agent finish), it
calls `sessions_yield(reason=...)`. The run loop catches the resulting
`abort_event` and ends the turn cleanly with `stop_reason="yielded"`
plus the operator's reason captured for logging / dashboard rendering.

This is intentionally simpler than openclaw's full implementation —
they tie it to a global session abort registry; we just trip the
RuntimeConfig.abort_event so the next iteration's check picks it up.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.tools_pkg._desc import hermes_desc


class _YieldArgs(BaseModel):
    model_config = {"extra": "forbid"}
    reason: str = Field(
        ...,
        min_length=1,
        description=(
            "Short human-readable reason for yielding (logged + shown "
            'to the operator). e.g. "waiting on user reply".'
        ),
    )


def yield_tool(*, abort_event: asyncio.Event | None = None) -> Tool:
    """Build a `sessions_yield` tool. Pass the same `abort_event` you
    plumb into `RuntimeConfig.abort_event`; the tool handler will set
    it so the run loop's next iteration check terminates the turn.

    When `abort_event` is None (e.g. unit tests) the tool returns a
    string but doesn't actually abort — useful for verifying the
    handler shape without spinning up a real run loop.
    """

    async def _h(args: _YieldArgs) -> str:
        if abort_event is not None:
            abort_event.set()
        return f"yielded: {args.reason}"

    return FunctionTool(
        name="sessions_yield",
        description=hermes_desc(
            "Voluntarily end the current turn early. The run loop stops "
            "cleanly with stop_reason='yielded'. Useful when waiting on a "
            "cron tick / user reply / sibling agent.",
            when_use=[
                "you've done all you can until an external trigger fires",
                "blocking would burn tokens with no progress",
            ],
            when_skip=[
                "you can still make progress with the tools you have",
                "you'd be yielding to avoid a hard problem (don't)",
            ],
            notes="Provide a short, specific `reason` for the operator log.",
        ),
        input_model=_YieldArgs,
        handler=_h,
    )


__all__ = ["yield_tool"]
