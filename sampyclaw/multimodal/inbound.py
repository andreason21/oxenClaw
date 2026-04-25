"""Normalize inbound `MediaItem` photos to a uniform `InboundImage`.

`MediaItem.source` can hold any of:

- `data:<mime>;base64,<payload>` — already inline; just split.
- `http(s)://...` — fetch via `security/net/guarded_session` (SSRF guards
  apply automatically).
- A bare file path or local URI — refused, since plugins should have
  resolved local files to bytes before reaching here.

The result is always `(media_type, raw_bytes, base64_str)` with a hard
size cap (`max_bytes`) and a MIME-type sniff so a misdeclared upload
doesn't get sent to the model.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass

from sampyclaw.plugin_sdk.channel_contract import MediaItem
from sampyclaw.plugin_sdk.runtime_env import get_logger

logger = get_logger("multimodal.inbound")

DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB — most provider limits sit at 5–20 MiB.

# JPEG/PNG/GIF/WebP magic-byte signatures keyed by MIME type.
_MIME_SNIFFERS: tuple[tuple[str, bytes], ...] = (
    ("image/jpeg", b"\xff\xd8\xff"),
    ("image/png", b"\x89PNG\r\n\x1a\n"),
    ("image/gif", b"GIF87a"),
    ("image/gif", b"GIF89a"),
    ("image/webp", b"RIFF"),  # full check below also requires "WEBP" at offset 8.
)

_DATA_URI_RE = re.compile(
    r"^data:(?P<mime>[\w/.+-]+);base64,(?P<payload>[A-Za-z0-9+/=]+)\s*$",
    re.IGNORECASE,
)

ALLOWED_MIME_PREFIXES: tuple[str, ...] = ("image/",)


class MediaNormalizationError(Exception):
    """Raised when a media item can't be safely turned into bytes."""


@dataclass(frozen=True)
class InboundImage:
    """Decoded, validated image ready for any provider."""

    media_type: str
    data_b64: str
    raw_bytes: bytes

    @property
    def size_bytes(self) -> int:
        return len(self.raw_bytes)

    def data_uri(self) -> str:
        return f"data:{self.media_type};base64,{self.data_b64}"


def _sniff_mime(raw: bytes) -> str | None:
    if len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    for mime, magic in _MIME_SNIFFERS:
        if raw.startswith(magic):
            return mime
    return None


def _validate_mime(declared: str | None, raw: bytes) -> str:
    sniffed = _sniff_mime(raw)
    if sniffed is None:
        raise MediaNormalizationError(
            "could not sniff image MIME type from payload "
            "(expected JPEG/PNG/GIF/WebP)"
        )
    if declared and declared.lower() != sniffed:
        # Trust the sniff over the declaration — clients lie about MIME.
        logger.debug(
            "declared mime %r does not match sniffed %r; using sniffed",
            declared,
            sniffed,
        )
    if not any(sniffed.startswith(p) for p in ALLOWED_MIME_PREFIXES):
        raise MediaNormalizationError(
            f"refused non-image MIME type: {sniffed!r}"
        )
    return sniffed


def _from_data_uri(source: str) -> tuple[str, bytes]:
    match = _DATA_URI_RE.match(source.strip())
    if match is None:
        raise MediaNormalizationError("malformed data: URI")
    mime = match.group("mime")
    try:
        raw = base64.b64decode(match.group("payload"), validate=True)
    except Exception as exc:
        raise MediaNormalizationError(
            f"data: URI base64 payload not decodable: {exc}"
        ) from exc
    return mime, raw


async def _fetch_url(url: str, *, max_bytes: int) -> bytes:
    """Fetch an image URL via the SSRF-guarded session.

    Raises `MediaNormalizationError` on transport / size / HTTP errors —
    callers wrap one item failure without dragging the whole turn down.
    """
    from sampyclaw.security.net.guarded_fetch import (
        guarded_session,
        policy_pre_flight,
    )
    from sampyclaw.security.net.policy import policy_from_env

    policy = policy_from_env()
    try:
        policy_pre_flight(url, policy)
    except Exception as exc:
        raise MediaNormalizationError(
            f"image URL refused by NetPolicy: {exc}"
        ) from exc
    try:
        async with guarded_session(policy) as session:
            async with session.get(url) as resp:
                if resp.status >= 400:
                    raise MediaNormalizationError(
                        f"image fetch returned HTTP {resp.status}"
                    )
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise MediaNormalizationError(
                            f"image exceeds max_bytes={max_bytes}"
                        )
                return bytes(buf)
    except MediaNormalizationError:
        raise
    except Exception as exc:
        raise MediaNormalizationError(
            f"image fetch failed: {exc}"
        ) from exc


async def normalize_media_item(
    item: MediaItem, *, max_bytes: int = DEFAULT_MAX_BYTES
) -> InboundImage:
    """Turn one `MediaItem` (kind=photo) into an `InboundImage`.

    Caller is responsible for filtering out non-photo kinds (`audio`,
    `voice`, `document`, etc.) — those need their own normalizers and
    aren't covered by image-input pipelines.
    """
    if item.kind != "photo":
        raise MediaNormalizationError(
            f"normalize_media_item only handles kind='photo' (got {item.kind!r})"
        )
    source = item.source.strip()
    if source.startswith("data:"):
        mime, raw = _from_data_uri(source)
    elif source.startswith(("http://", "https://")):
        raw = await _fetch_url(source, max_bytes=max_bytes)
        mime = item.mime_type or ""
    else:
        raise MediaNormalizationError(
            "MediaItem.source must be a data: URI or http(s) URL "
            "(plugins should embed bytes before reaching the agent)"
        )
    if len(raw) > max_bytes:
        raise MediaNormalizationError(
            f"image size {len(raw)} exceeds max_bytes={max_bytes}"
        )
    media_type = _validate_mime(mime, raw)
    return InboundImage(
        media_type=media_type,
        data_b64=base64.b64encode(raw).decode("ascii"),
        raw_bytes=raw,
    )


async def normalize_inbound_images(
    media: list[MediaItem],
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_images: int = 10,
) -> tuple[list[InboundImage], list[str]]:
    """Normalize every photo in `media`, collecting per-item failures.

    Returns `(images, dropped_reasons)` so callers can surface "couldn't
    use 1 of 3 images because …" to the agent in a text fallback.
    """
    images: list[InboundImage] = []
    dropped: list[str] = []
    photo_count = 0
    for idx, item in enumerate(media):
        if item.kind != "photo":
            continue
        photo_count += 1
        if photo_count > max_images:
            dropped.append(
                f"image #{idx + 1}: skipped (max_images={max_images} exceeded)"
            )
            continue
        try:
            images.append(
                await normalize_media_item(item, max_bytes=max_bytes)
            )
        except MediaNormalizationError as exc:
            dropped.append(f"image #{idx + 1}: {exc}")
            logger.debug(
                "skipping image #%d: %s", idx + 1, exc, exc_info=False
            )
    return images, dropped
