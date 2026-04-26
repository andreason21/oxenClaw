"""Media handling primitives exposed to channel plugins.

Port of openclaw `src/plugin-sdk/media-runtime.ts`, `media-mime.ts`, `outbound-media.ts`.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path


def guess_mime_type(path: str | Path) -> str:
    """Best-effort MIME guess from extension. Callers should prefer magic-byte sniffing for inbound bytes."""
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def is_image(mime: str) -> bool:
    return mime.startswith("image/")


def is_video(mime: str) -> bool:
    return mime.startswith("video/")


def is_audio(mime: str) -> bool:
    return mime.startswith("audio/")
