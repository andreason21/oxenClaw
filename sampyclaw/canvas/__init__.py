"""Canvas subsystem — dashboard-only HTML rendering surface for agents."""

from sampyclaw.canvas.errors import (
    CanvasError,
    CanvasEvalError,
    CanvasNotOpenError,
    CanvasResourceCapError,
)
from sampyclaw.canvas.events import (
    DEFAULT_QUEUE_SIZE,
    CanvasEvent,
    CanvasEventBus,
)
from sampyclaw.canvas.store import (
    ABSOLUTE_MAX_HTML_BYTES,
    DEFAULT_AGENT_CAPACITY,
    CanvasState,
    CanvasStore,
)

# Process-wide singletons. Gateway boots them via `register_canvas_methods`,
# tools register against the same instances via `_maybe_canvas_tools()`.
_default_store: CanvasStore | None = None
_default_bus: CanvasEventBus | None = None


def get_default_canvas_store() -> CanvasStore:
    global _default_store
    if _default_store is None:
        _default_store = CanvasStore()
    return _default_store


def get_default_canvas_bus() -> CanvasEventBus:
    global _default_bus
    if _default_bus is None:
        _default_bus = CanvasEventBus()
    return _default_bus


def reset_default_canvas() -> None:
    """Test-only: drop the singletons so each test starts clean."""
    global _default_store, _default_bus
    _default_store = None
    _default_bus = None


__all__ = [
    "ABSOLUTE_MAX_HTML_BYTES",
    "DEFAULT_AGENT_CAPACITY",
    "DEFAULT_QUEUE_SIZE",
    "CanvasError",
    "CanvasEvalError",
    "CanvasEvent",
    "CanvasEventBus",
    "CanvasNotOpenError",
    "CanvasResourceCapError",
    "CanvasState",
    "CanvasStore",
    "get_default_canvas_bus",
    "get_default_canvas_store",
    "reset_default_canvas",
]
