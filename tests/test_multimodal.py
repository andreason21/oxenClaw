"""Tests for `sampyclaw.multimodal`."""

from __future__ import annotations

import base64

import pytest

from sampyclaw.multimodal import (
    InboundImage,
    MediaNormalizationError,
    anthropic_image_block,
    google_image_part,
    model_supports_images,
    normalize_inbound_images,
    normalize_media_item,
    openai_image_url_block,
    pi_image_content,
)
from sampyclaw.plugin_sdk.channel_contract import MediaItem


# ─── capability gate ─────────────────────────────────────────────────


def test_model_supports_images_uses_catalog_for_known_model():
    # gemma4:latest is in the pi catalog with supports_image_input=True.
    assert model_supports_images("gemma4:latest") is True
    # gemma3:4b is in the catalog with supports_image_input=False.
    assert model_supports_images("gemma3:4b") is False


def test_model_supports_images_falls_back_to_substring_match():
    # Not in catalog but matches the heuristic list.
    assert model_supports_images("claude-sonnet-4-6") is True
    assert model_supports_images("gpt-4o-mini") is True
    assert model_supports_images("llama3.2-vision:11b") is True


def test_model_supports_images_false_for_text_only_model():
    assert model_supports_images("text-only-model") is False
    assert model_supports_images("") is False
    assert model_supports_images(None) is False


# ─── data: URI handling ──────────────────────────────────────────────


def _data_uri_jpeg() -> str:
    raw = b"\xff\xd8\xff" + b"x" * 64
    return f"data:image/jpeg;base64,{base64.b64encode(raw).decode()}"


@pytest.mark.asyncio
async def test_normalize_data_uri_jpeg():
    item = MediaItem(kind="photo", source=_data_uri_jpeg(), mime_type="image/jpeg")
    img = await normalize_media_item(item)
    assert img.media_type == "image/jpeg"
    assert img.size_bytes > 64
    assert img.data_uri().startswith("data:image/jpeg;base64,")


@pytest.mark.asyncio
async def test_normalize_data_uri_png():
    raw = b"\x89PNG\r\n\x1a\n" + b"x" * 32
    src = f"data:image/png;base64,{base64.b64encode(raw).decode()}"
    img = await normalize_media_item(
        MediaItem(kind="photo", source=src, mime_type="image/png")
    )
    assert img.media_type == "image/png"


@pytest.mark.asyncio
async def test_normalize_rejects_non_image_mime_after_sniff():
    """Even if the declared mime is wrong, sniff still controls."""
    # PDF magic, but declared as image/png → must reject (not image/*).
    raw = b"%PDF-1.4\n" + b"x" * 32
    src = f"data:image/png;base64,{base64.b64encode(raw).decode()}"
    with pytest.raises(MediaNormalizationError):
        await normalize_media_item(
            MediaItem(kind="photo", source=src, mime_type="image/png")
        )


@pytest.mark.asyncio
async def test_normalize_rejects_oversize_payload():
    # 11 MiB of zeros (still "looks like" PNG so sniff would pass) but
    # over the 10 MiB cap.
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * (11 * 1024 * 1024)
    src = f"data:image/png;base64,{base64.b64encode(raw).decode()}"
    with pytest.raises(MediaNormalizationError):
        await normalize_media_item(
            MediaItem(kind="photo", source=src, mime_type="image/png")
        )


@pytest.mark.asyncio
async def test_normalize_rejects_non_photo_kind():
    item = MediaItem(kind="audio", source=_data_uri_jpeg())
    with pytest.raises(MediaNormalizationError):
        await normalize_media_item(item)


@pytest.mark.asyncio
async def test_normalize_rejects_local_file_uri():
    item = MediaItem(kind="photo", source="file:///tmp/x.jpg")
    with pytest.raises(MediaNormalizationError):
        await normalize_media_item(item)


@pytest.mark.asyncio
async def test_normalize_inbound_images_collects_per_item_failures():
    good = MediaItem(kind="photo", source=_data_uri_jpeg(), mime_type="image/jpeg")
    bad = MediaItem(kind="photo", source="file:///etc/passwd")
    images, dropped = await normalize_inbound_images([good, bad])
    assert len(images) == 1
    assert len(dropped) == 1
    assert "file:///etc/passwd" in dropped[0] or "data:" in dropped[0]


@pytest.mark.asyncio
async def test_normalize_inbound_images_caps_at_max_images():
    items = [
        MediaItem(kind="photo", source=_data_uri_jpeg(), mime_type="image/jpeg")
        for _ in range(15)
    ]
    images, dropped = await normalize_inbound_images(items, max_images=10)
    assert len(images) == 10
    assert len(dropped) == 5
    assert all("max_images" in d for d in dropped)


@pytest.mark.asyncio
async def test_normalize_inbound_images_skips_non_photo_kinds_silently():
    photo = MediaItem(kind="photo", source=_data_uri_jpeg(), mime_type="image/jpeg")
    voice = MediaItem(kind="voice", source="ignored")
    images, dropped = await normalize_inbound_images([voice, photo, voice])
    assert len(images) == 1
    assert dropped == []


# ─── format builders ─────────────────────────────────────────────────


def _img() -> InboundImage:
    return InboundImage(media_type="image/jpeg", data_b64="QkFTRTY0", raw_bytes=b"BASE64")


def test_anthropic_image_block_shape():
    block = anthropic_image_block(_img())
    assert block == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": "QkFTRTY0",
        },
    }


def test_openai_image_url_block_shape():
    block = openai_image_url_block(_img())
    assert block["type"] == "image_url"
    assert block["image_url"]["url"] == "data:image/jpeg;base64,QkFTRTY0"


def test_google_image_part_shape():
    part = google_image_part(_img())
    assert part == {
        "inline_data": {
            "mime_type": "image/jpeg",
            "data": "QkFTRTY0",
        }
    }


def test_pi_image_content_shape():
    block = pi_image_content(_img())
    assert block.type == "image"
    assert block.media_type == "image/jpeg"
    assert block.data == "QkFTRTY0"
