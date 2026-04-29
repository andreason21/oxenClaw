"""LLM-callable canvas tools.

Render HTML on the dashboard's right-side canvas panel. The agent sees
the panel as the only place its visual output goes — no native node, no
external URL. The HTML lands in a sandboxed `srcdoc` iframe so cookies,
storage, and parent state are all out of reach.

Tools bundled by `default_canvas_tools`:

- `canvas_present(html, title?)` — replace the panel with `html`.
- `canvas_hide()` — collapse the panel.

Opt-in (not in the default bundle):

- `canvas_eval(expression, timeout_seconds=5)` — run a JS expression
  inside the open canvas iframe. The skill author MUST have wired a
  `message` listener in the HTML it presented; otherwise the call
  cleanly times out.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from oxenclaw.agents.tools import FunctionTool, Tool
from oxenclaw.canvas import (
    ABSOLUTE_MAX_HTML_BYTES,
    CanvasEvalError,
    CanvasEvent,
    CanvasEventBus,
    CanvasNotOpenError,
    CanvasResourceCapError,
    CanvasStore,
)
from oxenclaw.pi.tool_runtime import truncate_tool_result
from oxenclaw.plugin_sdk.runtime_env import get_logger
from oxenclaw.tools_pkg._desc import hermes_desc

logger = get_logger("tools.canvas")

# Tool-side cap is well below the absolute API cap; the model rarely
# needs more than ~64 KiB for the usual card / chart / mini-game.
DEFAULT_MAX_HTML_BYTES = 256 * 1024
DEFAULT_MAX_EVAL_RESULT_CHARS = 8 * 1024


class _PresentArgs(BaseModel):
    model_config = {"extra": "forbid"}
    html: str = Field(
        ...,
        description=(
            "Full HTML document to render on the dashboard canvas. Must "
            "begin with <!DOCTYPE html> or <html>. Self-contained — "
            "no external CSS/JS URLs."
        ),
    )
    title: str = Field(
        default="",
        max_length=120,
        description="Short title shown above the canvas panel.",
    )


class _HideArgs(BaseModel):
    model_config = {"extra": "forbid"}


class _EvalArgs(BaseModel):
    model_config = {"extra": "forbid"}
    expression: str = Field(
        ...,
        min_length=1,
        max_length=4096,
        description=(
            "JavaScript expression to evaluate inside the canvas iframe. "
            "The HTML you presented must include a "
            "window.addEventListener('message', ...) handler that runs "
            "the expression and replies via the provided MessagePort."
        ),
    )
    timeout_seconds: float = Field(
        default=5.0,
        gt=0.0,
        le=15.0,
        description="Hard deadline before the call returns 'no response'.",
    )


def canvas_present_tool(
    *,
    agent_id: str,
    store: CanvasStore,
    bus: CanvasEventBus,
    max_html_bytes: int = DEFAULT_MAX_HTML_BYTES,
) -> Tool:
    cap = min(max_html_bytes, ABSOLUTE_MAX_HTML_BYTES)

    async def _handler(args: _PresentArgs) -> str:
        size = len(args.html.encode("utf-8"))
        if size > cap:
            raise CanvasResourceCapError(f"html is {size} bytes, exceeds canvas cap {cap}")
        state = store.present(agent_id, html=args.html, title=args.title)
        bus.publish(
            CanvasEvent(
                kind="present",
                agent_id=agent_id,
                payload={"html": args.html, "title": args.title, "version": state.version},
            )
        )
        return f"canvas presented (version={state.version}, bytes={size})"

    return FunctionTool(
        name="canvas_present",
        description=hermes_desc(
            "Render an HTML page on the dashboard canvas panel.",
            when_use=[
                "the user asks to show / display / render / draw / chart / visualize",
                "you want to surface a chart, table, or mini-UI alongside text",
            ],
            when_skip=[
                "a plain-text reply suffices (don't open a panel for one number)",
                "the content needs external scripts/CSS (sandboxed iframe)",
            ],
            alternatives={"canvas_hide": "collapse the panel when done"},
            notes=(
                "HTML must be a self-contained document; no external CSS/JS "
                "URLs."
            ),
        ),
        input_model=_PresentArgs,
        handler=_handler,
    )


def canvas_hide_tool(
    *,
    agent_id: str,
    store: CanvasStore,
    bus: CanvasEventBus,
) -> Tool:
    async def _handler(_: _HideArgs) -> str:
        store.hide(agent_id)
        bus.publish(CanvasEvent(kind="hide", agent_id=agent_id, payload={}))
        return "canvas hidden"

    return FunctionTool(
        name="canvas_hide",
        description=hermes_desc(
            "Hide / collapse the dashboard canvas panel.",
            when_use=[
                "the rendered visual is no longer relevant",
                "the user asked to dismiss the canvas",
            ],
            when_skip=[
                "you're about to call canvas_present again — just present, no need to hide first",
            ],
        ),
        input_model=_HideArgs,
        handler=_handler,
    )


def canvas_eval_tool(
    *,
    agent_id: str,
    store: CanvasStore,
    bus: CanvasEventBus,
    max_result_chars: int = DEFAULT_MAX_EVAL_RESULT_CHARS,
) -> Tool:
    import asyncio

    async def _handler(args: _EvalArgs) -> str:
        state = store.get(agent_id)
        if state is None or state.hidden:
            raise CanvasNotOpenError("no visible canvas to evaluate; call canvas_present first")
        request_id = bus.new_eval_request_id()
        fut = bus.register_eval_waiter(request_id)
        bus.publish(
            CanvasEvent(
                kind="eval",
                agent_id=agent_id,
                request_id=request_id,
                payload={"expression": args.expression},
            )
        )
        try:
            value: Any = await asyncio.wait_for(fut, timeout=args.timeout_seconds)
        except TimeoutError as exc:
            bus.reject_eval(request_id, exc)
            raise CanvasEvalError(f"canvas_eval timed out after {args.timeout_seconds}s") from exc
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = repr(value)
        truncated, _ = truncate_tool_result(text, max_chars=max_result_chars)
        return truncated

    return FunctionTool(
        name="canvas_eval",
        description=hermes_desc(
            "Evaluate a JavaScript expression inside the currently-open "
            "canvas iframe; returns its JSON-stringified result.",
            when_use=[
                "the canvas HTML you wrote already wires a 'message' handler",
                "you need a value back from the iframe (form state, chart pick)",
            ],
            when_skip=[
                "no canvas is open (call canvas_present first)",
                "the iframe HTML has no message handler — call will time out",
            ],
            alternatives={"canvas_present": "render the HTML you'll later eval into"},
        ),
        input_model=_EvalArgs,
        handler=_handler,
    )


def default_canvas_tools(
    *,
    agent_id: str,
    store: CanvasStore,
    bus: CanvasEventBus,
) -> list[Tool]:
    """Always-safe canvas tool bundle. Excludes `canvas_eval` (opt-in)."""
    return [
        canvas_present_tool(agent_id=agent_id, store=store, bus=bus),
        canvas_hide_tool(agent_id=agent_id, store=store, bus=bus),
    ]


__all__ = [
    "DEFAULT_MAX_EVAL_RESULT_CHARS",
    "DEFAULT_MAX_HTML_BYTES",
    "canvas_eval_tool",
    "canvas_hide_tool",
    "canvas_present_tool",
    "default_canvas_tools",
]
