"""Multimodal (image input) support.

Cross-cutting helpers used by every agent provider that wants to feed
inbound photos to the model.

- `capabilities.py` — `model_supports_images()` — capability gate sourced
  from the pi catalog with a hard-coded fallback list of known
  multimodal models for providers that don't go through pi (e.g.
  LocalAgent pointed at a non-catalog Ollama tag).
- `inbound.py` — `InboundImage` dataclass + `normalize_media_item()`
  that turns a `MediaItem` into bytes/base64 with size + MIME guards.
- `formats.py` — provider-specific content-block builders so each
  agent serializes the same `InboundImage` into the shape its API
  expects.
"""

from sampyclaw.multimodal.capabilities import (
    KNOWN_IMAGE_MODELS,
    model_supports_images,
)
from sampyclaw.multimodal.formats import (
    anthropic_image_block,
    google_image_part,
    openai_image_url_block,
    pi_image_content,
)
from sampyclaw.multimodal.inbound import (
    InboundImage,
    MediaNormalizationError,
    normalize_inbound_images,
    normalize_media_item,
)

__all__ = [
    "InboundImage",
    "KNOWN_IMAGE_MODELS",
    "MediaNormalizationError",
    "anthropic_image_block",
    "google_image_part",
    "model_supports_images",
    "normalize_inbound_images",
    "normalize_media_item",
    "openai_image_url_block",
    "pi_image_content",
]
