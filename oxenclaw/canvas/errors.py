"""Canvas-subsystem error types."""

from __future__ import annotations


class CanvasError(RuntimeError):
    """Base class for canvas errors."""


class CanvasNotOpenError(CanvasError):
    """Raised when an action targets an agent with no current canvas state."""


class CanvasResourceCapError(CanvasError):
    """Raised when an HTML payload or eval expression exceeds policy caps."""


class CanvasEvalError(CanvasError):
    """Raised when canvas.eval times out or the dashboard reports failure."""


__all__ = [
    "CanvasError",
    "CanvasEvalError",
    "CanvasNotOpenError",
    "CanvasResourceCapError",
]
