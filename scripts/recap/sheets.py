"""Pillow drawing helpers shared by contact sheets, preview sheets and thumbnails."""
from __future__ import annotations

import glob
import math
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .common import PKG_DIR

# Heavy display faces first for thumbnails; plain bold faces for sheet labels.
HEAVY_FONT_FILES = [
    "C:/Windows/Fonts/impact.ttf",
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/Library/Fonts/Impact.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
    "C:/Windows/Fonts/ariblk.ttf",
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/Library/Fonts/Arial Black.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial_Black.ttf",
]
BOLD_FONT_FILES = [
    "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation-sans/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]


def _fc_match(pattern: str) -> str | None:
    if not shutil.which("fc-match"):
        return None
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", pattern], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out if out and os.path.isfile(out) else None


@lru_cache(maxsize=None)
def font_path(heavy: bool = False) -> str | None:
    """RECAP_FONT, then any font dropped into scripts/recap/fonts/, then common system fonts."""
    env = os.environ.get("RECAP_FONT")
    if heavy and env and os.path.isfile(os.path.expanduser(env)):
        return os.path.expanduser(env)
    bundled = sorted(glob.glob(str(PKG_DIR / "fonts" / "*.ttf")) + glob.glob(str(PKG_DIR / "fonts" / "*.otf")))
    if bundled:
        return bundled[0]
    if heavy:
        for path in HEAVY_FONT_FILES:
            if os.path.isfile(path):
                return path
        found = _fc_match("sans-serif:weight=210")
        if found and any(w in Path(found).name.lower() for w in ("black", "heavy", "extrabold", "bold")):
            return found
    for path in BOLD_FONT_FILES:
        if os.path.isfile(path):
            return path
    return _fc_match("sans-serif:bold")


@lru_cache(maxsize=64)
def get_font(size: int, heavy: bool = False):
    size = max(8, int(size))
    path = font_path(heavy)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def text_box(draw: ImageDraw.ImageDraw, text: str, font, stroke: int = 0) -> tuple[int, int]:
    left, top, right, bottom = draw.multiline_textbbox((0, 0), text, font=font, stroke_width=stroke, align="center")
    return right - left, bottom - top


def wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int, max_lines: int = 0) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if current and draw.textlength(trial, font=font) > max_width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1].split()
        while last and draw.textlength(" ".join(last) + " ...", font=font) > max_width:
            last.pop()
        lines[-1] = " ".join(last).rstrip(".,;:") + " ..."
    return lines


def draw_tag(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font,
             fill=(255, 255, 255), bg=(0, 0, 0), pad: tuple[int, int] = (7, 3), anchor_bottom: bool = False) -> None:
    """Small opaque label box at a tile corner."""
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    w, h = right - left + 2 * pad[0], bottom - top + 2 * pad[1]
    x, y = xy
    if anchor_bottom:
        y -= h
    draw.rectangle([x, y, x + w, y + h], fill=bg)
    draw.text((x + pad[0] - left, y + pad[1] - top), text, font=font, fill=fill)


def load_tile(path: Path, size: tuple[int, int]) -> Image.Image:
    try:
        with Image.open(path) as im:
            return im.convert("RGB").resize(size, Image.LANCZOS)
    except (OSError, ValueError):
        tile = Image.new("RGB", size, (70, 20, 20))
        ImageDraw.Draw(tile).text((10, 10), "no frame", font=get_font(18), fill=(255, 255, 255))
        return tile


def grid_sheet(tiles: list[dict], cols: int, tile_w: int, tile_h: int, header: str) -> Image.Image:
    """tiles: {"image": Path, "top": str, "bottom": str | None, "bottom_bg": (r, g, b)}."""
    gap, header_h = 6, 40
    rows = max(1, math.ceil(len(tiles) / cols))
    sheet = Image.new("RGB", (cols * tile_w + (cols + 1) * gap, header_h + rows * tile_h + (rows + 1) * gap), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    draw.text((gap + 4, 9), header, font=get_font(22), fill=(235, 235, 235))
    label_font = get_font(max(14, tile_w // 15))
    for i, tile in enumerate(tiles):
        row, col = divmod(i, cols)
        x = gap + col * (tile_w + gap)
        y = header_h + gap + row * (tile_h + gap)
        sheet.paste(load_tile(tile["image"], (tile_w, tile_h)), (x, y))
        draw_tag(draw, (x, y), tile["top"], label_font, fill=(255, 235, 59), bg=(0, 0, 0))
        if tile.get("bottom"):
            draw_tag(draw, (x, y + tile_h), tile["bottom"], label_font, fill=(255, 255, 255),
                     bg=tile.get("bottom_bg", (0, 0, 0)), anchor_bottom=True)
    return sheet


def draw_grid(image: Image.Image) -> Image.Image:
    """Overlay 0.1-step guide lines labelled with 0-1 coordinates (for placing thumbnail elements)."""
    base = image.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    w, h = base.size
    font = get_font(max(14, h // 40))
    for k in range(1, 10):
        x, y = w * k / 10, h * k / 10
        draw.line([(x, 0), (x, h)], fill=(255, 255, 255, 120), width=1)
        draw.line([(0, y), (w, y)], fill=(255, 255, 255, 120), width=1)
        label = f"{k / 10:.1f}"
        draw.text((x + 3, 3), label, font=font, fill=(255, 235, 59, 255), stroke_width=2, stroke_fill=(0, 0, 0, 255))
        draw.text((3, y + 3), label, font=font, fill=(255, 235, 59, 255), stroke_width=2, stroke_fill=(0, 0, 0, 255))
    return Image.alpha_composite(base, overlay).convert("RGB")
