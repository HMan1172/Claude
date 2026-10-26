"""Draws a simple text pin (1000 x 1500, Pinterest's 2:3 shape) for each approved pin.

These are made in code, not by an image model, so Pinterest's AI-image labels don't apply.
Swap in real product photos later for better results.
"""
import hashlib
import os
import textwrap

from PIL import Image, ImageDraw, ImageFont

W, H = 1000, 1500
PALETTES = [
    ("#1F3A5F", "#F4F1EA"), ("#2E5E4E", "#F7F3E8"), ("#7A2E3A", "#FBF4EE"),
    ("#3D3B6E", "#F2F0FA"), ("#5B4636", "#FAF5EF"), ("#1E4D5C", "#EEF6F7"),
]
_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]


def _font(size: int):
    for path in _FONT_PATHS:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def draw_pin(pin: dict, out_path: str) -> str:
    idx = int(hashlib.md5(pin["product_id"].encode()).hexdigest(), 16) % len(PALETTES)
    bg, fg = PALETTES[idx]
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)

    # headline
    font = _font(96)
    lines = textwrap.wrap(pin["overlay_text"], width=14)[:4]
    line_h = 116
    y = (H - line_h * len(lines)) // 2 - 80
    for line in lines:
        w = d.textlength(line, font=font)
        d.text(((W - w) / 2, y), line, font=font, fill=fg)
        y += line_h

    # divider and subtitle
    d.rectangle([W / 2 - 60, y + 30, W / 2 + 60, y + 38], fill=fg)
    sub_font = _font(42)
    for i, line in enumerate(textwrap.wrap(pin["title"], width=34)[:3]):
        w = d.textlength(line, font=sub_font)
        d.text(((W - w) / 2, y + 80 + i * 56), line, font=sub_font, fill=fg)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    img.save(out_path, "PNG")
    return out_path
