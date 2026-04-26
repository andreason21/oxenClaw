"""Provider-specific content-block builders for `InboundImage`.

Each helper returns the dict shape that the corresponding provider's
HTTP API expects when an image is part of a user message. Returned
shapes are `dict[str, Any]` so they slot directly into JSON payload
construction.

References:
- Anthropic Messages API: `{"type": "image", "source": {"type": "base64",
  "media_type": "...", "data": "..."}}`
- OpenAI / Ollama OpenAI-compatible: `{"type": "image_url",
  "image_url": {"url": "data:image/..;base64,.."}}`
- Google Generative AI: parts list with `{"inline_data": {"mime_type":
  "...", "data": "<base64>"}}`
- pi (`oxenclaw.pi.messages.ImageContent`): a Pydantic model — return
  it instead of a dict so it merges cleanly with the rest of the
  pi-typed pipeline.
"""

from __future__ import annotations

from typing import Any

from oxenclaw.multimodal.inbound import InboundImage


def anthropic_image_block(image: InboundImage) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": image.media_type,
            "data": image.data_b64,
        },
    }


def openai_image_url_block(image: InboundImage) -> dict[str, Any]:
    """OpenAI / Ollama OpenAI-shape `image_url` block.

    Ollama's OpenAI-compatible endpoint accepts `image_url.url` as a
    `data:` URI exactly like the official OpenAI API.
    """
    return {
        "type": "image_url",
        "image_url": {"url": image.data_uri()},
    }


def google_image_part(image: InboundImage) -> dict[str, Any]:
    return {
        "inline_data": {
            "mime_type": image.media_type,
            "data": image.data_b64,
        }
    }


def pi_image_content(image: InboundImage):  # type: ignore[no-untyped-def]
    """Build a pi `ImageContent` block.

    Imported lazily so this module remains importable without pulling
    the full pi runner dependency tree at import time.
    """
    from oxenclaw.pi.messages import ImageContent

    return ImageContent(
        media_type=image.media_type,
        data=image.data_b64,
    )


__all__ = [
    "anthropic_image_block",
    "google_image_part",
    "openai_image_url_block",
    "pi_image_content",
]
