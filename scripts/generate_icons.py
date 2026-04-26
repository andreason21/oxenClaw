#!/usr/bin/env python3
"""Generate the Tauri desktop icon set from a single in-script vector.

Tauri's `bundle.icon` array references three files:
    desktop/src-tauri/icons/32x32.png
    desktop/src-tauri/icons/128x128.png
    desktop/src-tauri/icons/icon.ico   (multi-size: 16/32/48/64/128/256)

These are placeholder marks until a real logo lands. Re-run this script
after editing `draw_lobster()` to refresh every output bundle.

Requires Pillow:
    pip install Pillow
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ICONS_DIR = Path(__file__).resolve().parents[1] / "desktop" / "src-tauri" / "icons"

ACCENT = (255, 102, 0, 255)  # sampyClaw orange
INK = (255, 255, 255, 255)


def draw_lobster(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = max(1, size // 16)
    d.ellipse([pad, pad, size - pad, size - pad], fill=ACCENT)
    cx, cy = size / 2, size / 2
    r = size * 0.28
    w = max(2, size // 12)
    d.arc([cx - r, cy - r * 1.2, cx + r, cy + r * 0.2], start=200, end=360, fill=INK, width=w)
    d.arc([cx - r, cy - r * 0.2, cx + r, cy + r * 1.2], start=20, end=180, fill=INK, width=w)
    return img


def main() -> None:
    ICONS_DIR.mkdir(parents=True, exist_ok=True)

    for size in (32, 128):
        draw_lobster(size).save(ICONS_DIR / f"{size}x{size}.png", "PNG")

    # PIL downsamples from the source image to every requested ICO entry,
    # so start from 256 to populate every smaller size cleanly.
    base = draw_lobster(256)
    base.save(
        ICONS_DIR / "icon.ico",
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    # Generic icon.png used by AppImage / .deb desktop entries.
    draw_lobster(512).save(ICONS_DIR / "icon.png", "PNG")

    for p in sorted(ICONS_DIR.iterdir()):
        print(f"  {p.name:20s} {p.stat().st_size:>8d} B")


if __name__ == "__main__":
    main()
