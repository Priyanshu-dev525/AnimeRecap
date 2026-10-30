"""Thumbnails: edit a REAL frame to 1280x720 with Pillow. Never generates new art."""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageEnhance, ImageFilter, ImageStat

from .common import REFERENCES_DIR, Project, ToolError
from .media import extract_frame
from .sheets import draw_grid, get_font, grid_sheet, wrap

THUMB_W, THUMB_H = 1280, 720
SS = 2                      # draw at 2x and downscale for smooth edges
MAX_ELEMENTS = 4
YELLOW, GREEN, RED = (255, 212, 0), (57, 230, 70), (227, 22, 27)
BLACK, WHITE = (0, 0, 0, 255), (255, 255, 255, 255)


# ---------------------------------------------------------------- candidates

def _brightness(proj: Project, scene: dict) -> float:
    """Saturation and brightness of the keyframe: bright, clean frames make better thumbnails."""
    try:
        with Image.open(proj.keyframe(scene)) as im:
            _, sat, val = ImageStat.Stat(im.convert("RGB").resize((160, 90)).convert("HSV")).mean
    except OSError:
        return 0.0
    return 0.6 * sat / 255 + 0.4 * val / 255


def get_thumbnail_candidates(args: dict) -> dict:
    proj = Project.from_args(args)
    count = max(4, min(int(args.get("count", 12)), 20))
    pool = [s for s in proj.scenes if proj.usable(s)]
    if not pool:
        raise ToolError("There are no usable scenes.")
    top = sorted(pool, key=lambda s: -(s.get("importance") or 0))[:max(count * 4, 40)]
    ranked = sorted(top, key=lambda s: (-(s.get("importance") or 0), -_brightness(proj, s)))
    picked: list[dict] = []
    for scene in ranked:  # skip direct neighbours of a pick (near-duplicate frames) ...
        if len(picked) < count and all(abs(scene["id"] - p["id"]) > 1 for p in picked):
            picked.append(scene)
    for scene in ranked:  # ... unless that leaves too few candidates
        if len(picked) < count and scene not in picked:
            picked.append(scene)
    picked.sort(key=lambda s: s["id"])
    tiles = [{"image": proj.keyframe(s), "top": f"#{s['id']}  {proj.label_time(s)}",
              "bottom": f"imp {s.get('importance') or '?'}", "bottom_bg": (20, 90, 160)} for s in picked]
    out = proj.path("sheets", "thumbnail_candidates.jpg")
    out.parent.mkdir(parents=True, exist_ok=True)
    grid_sheet(tiles, 4, 400, 225, f"{proj.id}  |  thumbnail candidates").save(out, quality=88)
    images, reference = [out], None
    source = REFERENCES_DIR / "thumbnail_reference.webp"
    if source.is_file():
        reference = proj.path("sheets", "thumbnail_reference.jpg")
        try:
            with Image.open(source) as im:
                im.convert("RGB").save(reference, quality=92)
            images.append(reference)
        except OSError:
            reference = None
    return {
        "candidates": [{"scene": s["id"], "time": proj.label_time(s), "importance": s.get("importance"),
                        "description": s.get("description") or ""} for s in picked],
        "reference": str(reference) if reference else None,
        "next": "Pick one frame with ONE clear hero, then make_thumbnail with grid=true to read coordinates.",
        "_images": images,
    }


# ---------------------------------------------------------------- helpers

def _xy(value, name: str, width: float, height: float) -> tuple[float, float]:
    try:
        x, y = float(value[0]), float(value[1])
    except (TypeError, ValueError, IndexError, KeyError):
        raise ToolError(f"{name} must be [x, y] with values from 0 to 1 (fractions of the width and height).",
                        hint="Run make_thumbnail with grid=true to read coordinates off the picture.")
    if not (0 <= x <= 1 and 0 <= y <= 1):
        raise ToolError(f"{name} {value} is outside the picture; use values from 0 to 1.")
    return x * width, y * height


def _color(value, default: tuple) -> tuple:
    if not value:
        return default
    try:
        return ImageColor.getrgb(str(value))[:3]
    except ValueError:
        raise ToolError(f"Unknown color '{value}'.", hint="Use a name like yellow/white/red or a hex value like #FFD400.")


def _fit(text: str, size: int, max_width: float, stroke_ratio: float):
    """Largest heavy font (starting at `size`) whose text fits in max_width. Returns (font, stroke, w, h)."""
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    while True:
        font = get_font(size, heavy=True)
        stroke = max(2, int(size * stroke_ratio))
        left, top, right, bottom = probe.multiline_textbbox((0, 0), text, font=font, stroke_width=stroke, align="center")
        if right - left <= max_width or size <= 20:
            # Older Pillow returns float boxes; image sizes must be ints.
            return font, stroke, math.ceil(right - left), math.ceil(bottom - top)
        size = int(size * 0.9)


def _paste_centered(layer: Image.Image, item: Image.Image, cx: float, cy: float) -> None:
    x = int(min(max(0, cx - item.width / 2), max(0, layer.width - item.width)))
    y = int(min(max(0, cy - item.height / 2), max(0, layer.height - item.height)))
    layer.alpha_composite(item, (x, y))


def _rotated(item: Image.Image, el: dict) -> Image.Image:
    angle = float(el.get("angle", 0) or 0)
    return item.rotate(angle, resample=Image.BICUBIC, expand=True) if angle else item


def _shadow(layer: Image.Image, offset=(5, 6), blur: int = 6, opacity: float = 0.55) -> Image.Image:
    alpha = layer.getchannel("A").filter(ImageFilter.GaussianBlur(blur)).point(lambda a: int(a * opacity))
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    shadow.paste(Image.new("RGBA", layer.size, BLACK), offset, alpha)
    return shadow


# ---------------------------------------------------------------- elements

def _arrow(layer: Image.Image, el: dict) -> None:
    """Thick arrow with a black outline; the tip sits exactly on `to`."""
    w, h = layer.size
    x0, y0 = _xy(el.get("from"), "arrow 'from'", w, h)
    x1, y1 = _xy(el.get("to"), "arrow 'to'", w, h)
    length = math.hypot(x1 - x0, y1 - y0)
    if length < 0.06 * w:
        raise ToolError("The arrow is too short; start it farther away from what it points at.")
    shaft = float(el.get("width", 0.032)) * w
    ux, uy = (x1 - x0) / length, (y1 - y0) / length
    nx, ny = -uy, ux
    head_len, head_half, half = min(shaft * 2.3, length * 0.6), shaft * 1.35, shaft / 2
    bx, by = x1 - ux * head_len, y1 - uy * head_len
    pts = [(x0 + nx * half, y0 + ny * half), (bx + nx * half, by + ny * half),
           (bx + nx * head_half, by + ny * head_half), (x1, y1),
           (bx - nx * head_half, by - ny * head_half), (bx - nx * half, by - ny * half),
           (x0 - nx * half, y0 - ny * half)]
    outline = max(6.0, shaft * 0.22)
    draw = ImageDraw.Draw(layer)
    draw.polygon(pts, fill=BLACK)
    draw.line(pts + [pts[0]], fill=BLACK, width=int(2 * outline), joint="curve")
    for x, y in pts:
        draw.ellipse([x - outline, y - outline, x + outline, y + outline], fill=BLACK)
    draw.polygon(pts, fill=_color(el.get("color"), YELLOW) + (255,))


def _label(layer: Image.Image, el: dict) -> None:
    """Bold label tag: heavy text on a coloured box (bg 'none' for outlined text only)."""
    w, h = layer.size
    text = str(el.get("text") or "").strip()
    if not text:
        raise ToolError("A label needs text (1-4 words).")
    cx, cy = _xy(el.get("at"), "label 'at'", w, h)
    bg_value = el.get("bg", "#E3161B")
    bg = None if str(bg_value).lower() in ("none", "", "transparent") else _color(bg_value, RED)
    size = int(float(el.get("size", 0.11)) * h)
    font, stroke, tw, th = _fit(text, size, 0.9 * w, 0.06 if bg else 0.1)
    border = max(4, size // 16)
    pad_x, pad_y = int(size * 0.32), int(size * 0.2)
    tag = Image.new("RGBA", (tw + 2 * (pad_x + border), th + 2 * (pad_y + border)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tag)
    if bg:
        draw.rounded_rectangle([0, 0, tag.width - 1, tag.height - 1], radius=int(size * 0.18),
                               fill=bg + (255,), outline=BLACK, width=border)
    draw.multiline_text((tag.width / 2, tag.height / 2), text, font=font, anchor="mm", align="center",
                        fill=_color(el.get("color"), (255, 255, 255)) + (255,), stroke_width=stroke, stroke_fill=BLACK)
    _paste_centered(layer, _rotated(tag, el), cx, cy)


def _badge(layer: Image.Image, el: dict) -> None:
    """Glowing green game-style text such as +99 or Lv. 999."""
    w, h = layer.size
    text = str(el.get("text") or "+99").strip()
    cx, cy = _xy(el.get("at"), "badge 'at'", w, h)
    size = int(float(el.get("size", 0.14)) * h)
    color = _color(el.get("color"), GREEN)
    font, stroke, tw, th = _fit(text, size, 0.6 * w, 0.09)
    pad = int(size * 0.4)
    glow = Image.new("RGBA", (tw + 2 * pad, th + 2 * pad), (0, 0, 0, 0))
    center = (glow.width / 2, glow.height / 2)
    ImageDraw.Draw(glow).multiline_text(center, text, font=font, anchor="mm", align="center", fill=color + (255,),
                                        stroke_width=stroke + max(2, size // 16), stroke_fill=color + (255,))
    badge = glow.filter(ImageFilter.GaussianBlur(max(2, size // 8)))
    ImageDraw.Draw(badge).multiline_text(center, text, font=font, anchor="mm", align="center", fill=color + (255,),
                                         stroke_width=stroke, stroke_fill=BLACK)
    _paste_centered(layer, _rotated(badge, el), cx, cy)


def _bubble(layer: Image.Image, el: dict) -> None:
    """White speech bubble with a black outline and a tail pointing at the speaker."""
    w, h = layer.size
    text = str(el.get("text") or "").strip()
    if not text:
        raise ToolError("A bubble needs text (a few words).")
    cx, cy = _xy(el.get("at"), "bubble 'at'", w, h)
    size = int(float(el.get("size", 0.055)) * h)
    draw = ImageDraw.Draw(layer)
    font = get_font(size, heavy=True)
    body = "\n".join(wrap(draw, text, font, int(0.3 * w)))
    left, top, right, bottom = draw.multiline_textbbox((0, 0), body, font=font, align="center")
    rx, ry = (right - left) * 0.62 + size * 0.8, (bottom - top) * 0.68 + size * 0.7
    cx, cy = min(max(cx, rx + 8), w - rx - 8), min(max(cy, ry + 8), h - ry - 8)
    outline = max(4, size // 7)
    tail = None
    if el.get("tail") is not None:
        tx, ty = _xy(el.get("tail"), "bubble 'tail'", w, h)
        angle = math.atan2(ty - cy, tx - cx)
        tail = [(cx + rx * 0.8 * math.cos(angle + d), cy + ry * 0.8 * math.sin(angle + d)) for d in (0.3, -0.3)]
        tail.insert(1, (tx, ty))
        draw.polygon(tail, fill=BLACK)
        draw.line(tail + [tail[0]], fill=BLACK, width=2 * outline, joint="curve")
    draw.ellipse([cx - rx - outline, cy - ry - outline, cx + rx + outline, cy + ry + outline], fill=BLACK)
    if tail:
        draw.polygon(tail, fill=WHITE)
    draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=WHITE)
    draw.multiline_text((cx, cy), body, font=font, fill=BLACK, anchor="mm", align="center")


PAINTERS = {"arrow": _arrow, "label": _label, "badge": _badge, "bubble": _bubble}


def _draw_element(img: Image.Image, el: dict) -> Image.Image:
    if not isinstance(el, dict):
        raise ToolError("Each element must be an object with a type.")
    kind = str(el.get("type", "")).lower()
    if kind not in PAINTERS:
        raise ToolError(f"Unknown element type '{kind}'.", hint="Use arrow, label, badge or bubble.")
    layer = Image.new("RGBA", (THUMB_W * SS, THUMB_H * SS), (0, 0, 0, 0))
    PAINTERS[kind](layer, el)
    layer = layer.resize((THUMB_W, THUMB_H), Image.LANCZOS)
    base = Image.alpha_composite(img.convert("RGBA"), _shadow(layer))
    return Image.alpha_composite(base, layer).convert("RGB")


def _crop_zoom(frame: Image.Image, zoom: float, focus: tuple[float, float]) -> Image.Image:
    w, h = frame.size
    cw, ch = w / zoom, h / zoom
    left = min(max(0.0, focus[0] - cw / 2), w - cw)
    top = min(max(0.0, focus[1] - ch / 2), h - ch)
    return frame.crop((round(left), round(top), round(left + cw), round(top + ch))).resize((THUMB_W, THUMB_H), Image.LANCZOS)


def make_thumbnail(args: dict) -> dict:
    proj = Project.from_args(args)
    scene = proj.scene(args.get("scene_id"))
    elements = args.get("elements") or []
    if not isinstance(elements, list):
        raise ToolError("elements must be a list.")
    if len(elements) > MAX_ELEMENTS:
        raise ToolError(f"{len(elements)} elements is too many; use at most {MAX_ELEMENTS} and keep faces uncovered.")
    zoom = min(3.0, max(1.0, float(args.get("zoom", 1.0) or 1.0)))
    at = min(1.0, max(0.0, float(args.get("at", 0.5))))
    video = proj.video(scene["episode"])
    source = proj.path("frames", f"thumb_{scene['id']:05d}_{int(at * 100):03d}.png")
    if not source.exists():
        seconds = scene["start"] + (scene["end"] - scene["start"]) * at
        extract_frame(Path(video["path"]), seconds, source, 1920, 1080, video["sar"])
    with Image.open(source) as im:
        frame = im.convert("RGB")
    image = _crop_zoom(frame, zoom, _xy(args.get("focus", [0.5, 0.5]), "focus", *frame.size))
    if args.get("enhance", True):
        image = ImageEnhance.Color(image).enhance(1.25)
        image = ImageEnhance.Contrast(image).enhance(1.08)
        image = ImageEnhance.Brightness(image).enhance(1.04)
        image = ImageEnhance.Sharpness(image).enhance(1.4)
    for el in elements:
        image = _draw_element(image, el)
    out = proj.path("output", "thumbnail.jpg")
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, quality=92, optimize=True)
    shown = out
    if args.get("grid"):
        shown = proj.path("sheets", "thumbnail_grid.jpg")
        draw_grid(image).save(shown, quality=90)
    return {"thumbnail": str(out), "size": f"{THUMB_W}x{THUMB_H}", "scene_id": scene["id"], "zoom": zoom,
            "elements": len(elements),
            "next": "Look at the image. Faces must stay uncovered; adjust positions and run again if needed.",
            "_images": [shown]}
