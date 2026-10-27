"""Draws each approved pin as a 1000 x 1500 image (Pinterest's 2:3 shape).

With a product photo (image_url), the photo fills the top and the headline sits in a band below;
without one, or if the photo can't be fetched, it's a text-only design. Made in code, not by an
image model, so Pinterest's AI-image labels don't apply.
"""
import hashlib
import io
import os
import textwrap

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .dataflows import http

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


def _centered(d, lines, font, y, line_h, fill):
    for line in lines:
        d.text(((W - d.textlength(line, font=font)) / 2, y), line, font=font, fill=fill)
        y += line_h
    return y


def _photo_pin(pin: dict, bg: str, fg: str) -> Image.Image:
    photo = Image.open(io.BytesIO(http.request("GET", pin["image_url"], timeout=20).content)).convert("RGB")
    photo = ImageOps.contain(photo, (W - 80, 1000 - 80))  # whole product visible, never cropped
    img = Image.new("RGB", (W, H), "white")
    img.paste(photo, ((W - photo.width) // 2, (1000 - photo.height) // 2))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 1000, W, H], fill=bg)
    head = textwrap.wrap(pin["overlay_text"], width=20)[:2]
    sub = textwrap.wrap(pin["title"], width=40)[:2]
    y = 1000 + (500 - (len(head) * 88 + 30 + len(sub) * 50)) // 2  # centred in the band
    y = _centered(d, head, _font(72), y, 88, fg)
    _centered(d, sub, _font(38), y + 30, 50, fg)
    return img


def draw_pin(pin: dict, out_path: str) -> str:
    idx = int(hashlib.md5(pin["product_id"].encode()).hexdigest(), 16) % len(PALETTES)
    bg, fg = PALETTES[idx]
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    if pin.get("image_url"):
        try:
            _photo_pin(pin, bg, fg).save(out_path, "PNG")
            return out_path
        except Exception:  # unreachable or unreadable photo: use the text design instead
            pass
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

    img.save(out_path, "PNG")
    return out_path
