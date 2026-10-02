"""
Animated punishment card (Pillow only - no Discord code in here).

render_card_gif(...) returns GIF bytes: the same ELT punishment card as
punishment_card.py, drawn on top of an animated background (card_bg.gif),
plus a light-sweep and a blinking "recording" dot.

Keep this file next to punishment_card.py, card_bg.gif and the fonts/ folder.
NOTE: rendering is CPU heavy -> call it with `await asyncio.to_thread(...)`
AFTER `await interaction.response.defer()` (see the bot snippet in the reply).
"""

import io
import math
import os
import threading
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageSequence

from punishment_card import (
    FONT_DIR, H, RES, S, TYPE_STYLE, W, WEIGHT_NAMES,
    draw_avatar, draw_bar, draw_fields, draw_ornament, draw_stamp, has_duration,
    font, lin_grad, put, put_text, px, text_w,
)

HERE = os.path.dirname(os.path.abspath(__file__))
BG_PATH = os.environ.get("CARD_BG_PATH") or os.path.join(HERE, "card_bg.gif")

MAX_BYTES = 7_500_000            # stay well under Discord's upload limit
SCALES = (0.85, 0.7, 0.6)        # first try is already slightly smaller = faster + smaller
BG_BRIGHTNESS = 0.62
MAX_FRAMES = 36                  # cap on animation frames (speed + file size)
TRANSPARENT = 255                # palette slot reserved for the rounded corners

# Pillow font objects are not safe to use from several threads at once,
# so only one card is rendered at a time.
_RENDER_LOCK = threading.Lock()


# ------------------------------ fonts check ------------------------------
def _check_fonts():
    missing = []
    for family, weights in (("Orbitron", (700, 800, 900)), ("Rajdhani", (700,))):
        variable = os.path.join(FONT_DIR, f"{family}[wght].ttf")
        for wt in weights:
            static = os.path.join(FONT_DIR, f"{family}-{WEIGHT_NAMES[family][wt]}.ttf")
            if not (os.path.exists(static) or os.path.exists(variable)):
                missing.append(os.path.basename(static))
    if missing:
        print(f"⚠️ Card fonts missing in '{FONT_DIR}': {', '.join(missing)} - the card will use a plain fallback font.")


_check_fonts()


# ------------------------------ text cleanup ------------------------------
@lru_cache(maxsize=2048)
def _glyph_ok(ch: str) -> bool:
    f = font("Rajdhani", 700, 28)

    def glyph(c):
        im = Image.new("L", (80, 90), 0)
        ImageDraw.Draw(im).text((10, 10), c, font=f, fill=255)
        return im.tobytes()

    return ch == " " or glyph(ch) != glyph("\U0010FFFF")


def clean_for_card(text: str) -> str:
    """Removes characters the card font can't draw (emoji, Arabic...) so no empty boxes show up."""
    text = " ".join((text or "").split())
    if not text:
        return ""
    try:
        kept = [ch for ch in text if _glyph_ok(ch)]
    except Exception:
        return text
    return " ".join("".join(kept).split())


# ------------------------------ background ------------------------------
@lru_cache(maxsize=1)
def _load_bg(path: str):
    """Loads the GIF once, already cropped/resized to the card size (saves a lot of RAM + time)."""
    im = Image.open(path)
    frames, durations = [], []
    for fr in ImageSequence.Iterator(im):
        durations.append(max(20, fr.info.get("duration", 100) or 100))
        frames.append(ImageOps.fit(fr.convert("RGB"), (W, H), RES.LANCZOS))
    return tuple(frames), tuple(durations)


@lru_cache(maxsize=2)
def _prepared_bg(path: str, fw: int, fh: int, step: int):
    """Darkened + resized background frames, cached per (size, step) so retries don't redo the work."""
    frames, _ = _load_bg(path)
    table = [int(i * BG_BRIGHTNESS) for i in range(256)] * 3
    out = []
    for i in range(0, len(frames), step):
        f = frames[i]
        if (fw, fh) != (W, H):
            f = f.resize((fw, fh), RES.LANCZOS)
        out.append(f.point(table))
    return tuple(out)


# ------------------------------ static overlay ------------------------------
def _panel(img, x, y, w, h, radius, fill, outline=None):
    layer = Image.new("RGBA", (px(w), px(h)), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle((0, 0, layer.width - 1, layer.height - 1), px(radius), fill=fill,
                        outline=outline, width=px(1.5) if outline else 0)
    put(img, layer, px(x), px(y))


def _brackets(img, inset=16, length=34, thick=3, color=(181, 107, 255, 220)):
    glow = Image.new("RGBA", img.size, color[:3] + (0,))
    lay = Image.new("RGBA", img.size, color[:3] + (0,))
    gd, ld = ImageDraw.Draw(glow), ImageDraw.Draw(lay)
    i, ln, t = px(inset), px(length), px(thick)
    w, h = img.size
    for (cx, cy, sx, sy) in ((i, i, 1, 1), (w - i, i, -1, 1), (i, h - i, 1, -1), (w - i, h - i, -1, -1)):
        for d in (gd, ld):
            d.line((cx, cy, cx + sx * ln, cy), fill=color, width=t)
            d.line((cx, cy, cx, cy + sy * ln), fill=color, width=t)
    put(img, glow.filter(ImageFilter.GaussianBlur(px(4))), 0, 0)
    put(img, lay, 0, 0)


def _build_overlay(username, punisher, reason, ptype, case_no, date_text, avatar_bytes):
    color, sev = TYPE_STYLE[ptype]
    w, h = W * S, H * S
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))

    put(img, lin_grad(w, h, [(0, (12, 3, 28, 165)), (0.55, (20, 6, 44, 120)), (1, (9, 2, 20, 165))], 135), 0, 0)

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), px(22), fill=255)
    pad = px(80)
    big = Image.new("L", (w + 2 * pad, h + 2 * pad), 0)
    big.paste(mask, (pad, pad))
    big = big.filter(ImageFilter.GaussianBlur(px(40) / 2)).crop((pad, pad, pad + w, pad + h))
    edge = Image.new("RGBA", (w, h), (122, 44, 255, 0))
    edge.putalpha(ImageChops.invert(big).point(lambda v: int(v * 0x88 / 255)))
    put(img, edge, 0, 0)

    lines = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lines)
    for y in range(0, h, px(5)):
        ld.rectangle((0, y, w, y + px(2) - 1), fill=(255, 255, 255, 6))
    put(img, lines, 0, 0)

    _panel(img, 28, 84, 250, 358, 18, (10, 3, 22, 150), (139, 61, 255, 90))
    _panel(img, 288, 238, 590, 292 if has_duration(reason) else 228, 18, (10, 3, 22, 140), (139, 61, 255, 70))

    tf = font("Orbitron", 700, 20)
    put_text(img, px(40), px(49), "ELT COMMUNITY", tf, (255, 255, 255, 255), sp=6, glow=((181, 107, 255), 14))
    tag = "// PUNISHMENT LOG"
    put_text(img, px(W - 40) - text_w(tag, tf, 6), px(49), tag, tf, (199, 163, 255, 255), sp=6)

    put_text(img, px(300), px(132), "NEW", font("Orbitron", 900, 62), (255, 255, 255, 255), sp=4,
             glow=((181, 107, 255), 32))
    put_text(img, px(300), px(172), "PUNISHMENT", font("Orbitron", 900, 30), (255, 92, 240, 255), sp=14,
             glow=((255, 92, 240), 14))

    draw_avatar(img, avatar_bytes)
    draw_ornament(img, color)
    draw_bar(img, sev, color)
    draw_fields(img, username, punisher, reason)
    draw_stamp(img, ptype, color)

    put_text(img, px(300), px(H - 40), date_text, font("Orbitron", 700, 26), (199, 163, 255, 255), sp=3)
    sf, ef = font("Orbitron", 800, 14), font("Orbitron", 800, 26)
    put_text(img, px(W - 40) - text_w("ISSUED BY", sf, 5), px(H - 66), "ISSUED BY", sf, (139, 111, 192, 255), sp=5)
    put_text(img, px(W - 40) - text_w("ELITE", ef, 0), px(H - 38), "ELITE", ef, (255, 255, 255, 255),
             glow=((181, 107, 255), 12))

    _brackets(img)

    ring = Image.new("RGBA", (w, h), (139, 61, 255, 0))
    ba = Image.new("L", (w, h), 0)
    ImageDraw.Draw(ba).rounded_rectangle((0, 0, w - 1, h - 1), px(22), outline=255, width=px(2))
    ring.putalpha(ba)
    put(img, ring, 0, 0)

    tag_left = W - 40 - text_w(tag, tf, 6) / S
    return img.resize((W, H), RES.LANCZOS), tag_left


def _blink_dot(color=(255, 92, 240)):
    size = 40
    dot = Image.new("RGBA", (size, size), color + (0,))
    ImageDraw.Draw(dot).ellipse((size / 2 - 6, size / 2 - 6, size / 2 + 6, size / 2 + 6), fill=color + (255,))
    glow = dot.filter(ImageFilter.GaussianBlur(6))
    glow.alpha_composite(dot)
    return glow


def _sweep_band(fw, fh):
    bw = int(fw * 0.20)
    band = lin_grad(bw, fh, [(0, (210, 160, 255, 0)), (0.5, (225, 190, 255, 44)), (1, (210, 160, 255, 0))], 90)
    shear = 0.35
    ow = bw + int(fh * shear)
    return band.transform((ow, fh), Image.AFFINE, (1, shear, -fh * shear, 0, 1, 0), resample=Image.BILINEAR)


# ------------------------------ encoding ------------------------------
def _encode(overlay, tag_left, durations, scale, step):
    fw, fh = int(round(W * scale)), int(round(H * scale))
    if scale != 1.0:
        overlay = overlay.resize((fw, fh), RES.LANCZOS)

    bg = _prepared_bg(BG_PATH, fw, fh, step)
    n = len(bg)
    band = _sweep_band(fw, fh)
    dot = _blink_dot()
    if scale != 1.0:
        dot = dot.resize((max(1, int(dot.width * scale)), max(1, int(dot.height * scale))), RES.LANCZOS)
    dot_xy = (int((tag_left - 16) * scale) - dot.width // 2, int(42 * scale) - dot.height // 2)

    corner = Image.new("L", (W * 2, H * 2), 0)
    ImageDraw.Draw(corner).rounded_rectangle((0, 0, W * 2 - 1, H * 2 - 1), 44, fill=255)
    corner = corner.resize((fw, fh), RES.LANCZOS)
    hole = corner.point(lambda v: 255 if v < 128 else 0)

    def compose(k):
        base = bg[k].convert("RGBA")
        base.alpha_composite(overlay)
        t = k / max(1, n)
        if t < 0.75:
            x = int(-band.width + (fw + band.width) * (t / 0.75))
            put(base, band, x, 0)
        if (k * 4 // n) % 2 == 0:
            put(base, dot, *dot_xy)
        return base.convert("RGB")

    # one shared palette for the whole GIF
    samples = [compose(min(n - 1, int(n * f))) for f in (0.0, 0.25, 0.5, 0.75)]
    tile = (fw // 2, fh // 2)
    montage = Image.new("RGB", (tile[0] * 2, tile[1] * 2))
    for k, s in enumerate(samples):
        montage.paste(s.resize(tile, RES.BILINEAR), ((k % 2) * tile[0], (k // 2) * tile[1]))
    pal_src = montage.quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    colors = pal_src.getpalette()[:255 * 3]
    colors += [0, 0, 0] * (256 - len(colors) // 3)
    pal = Image.new("P", (1, 1))
    pal.putpalette(colors)

    # BUGFIX: slot 255 is the "transparent" slot. Real pixels must never land on it,
    # otherwise pure-black pixels turn into see-through holes.
    no_transparent = [i if i != TRANSPARENT else TRANSPARENT - 1 for i in range(256)]

    out_frames = []
    for k in range(n):
        q = compose(k).quantize(palette=pal, dither=0)
        q = q.point(no_transparent)
        q.paste(TRANSPARENT, mask=hole)
        out_frames.append(q)

    durs = []
    for k in range(n):
        i = k * step
        d = sum(durations[i:i + step])
        durs.append(max(20, int(round(d / 10.0)) * 10))      # GIF timing is in 10 ms units

    buf = io.BytesIO()
    out_frames[0].save(
        buf, format="GIF", save_all=True, append_images=out_frames[1:],
        duration=durs, loop=0, transparency=TRANSPARENT, disposal=2, optimize=False,
    )
    return buf.getvalue()


def render_card_gif(username, punisher, reason, ptype, case_no, date_text,
                    avatar_bytes=None, max_bytes=MAX_BYTES) -> bytes:
    """Returns an animated GIF. Blocking + CPU heavy: run it in a thread (asyncio.to_thread).
    Raises FileNotFoundError if card_bg.gif is missing."""
    ptype = (ptype or "").upper()
    if ptype not in TYPE_STYLE:                                   # BUGFIX: unknown type used to crash with KeyError
        ptype = "WARNING"
    username, punisher = clean_for_card(username) or "Unknown", clean_for_card(punisher) or "Unknown"
    reason = clean_for_card(reason) or "-"

    with _RENDER_LOCK:
        bg_frames, durations = _load_bg(BG_PATH)
        step = max(1, math.ceil(len(bg_frames) / MAX_FRAMES))
        overlay, tag_left = _build_overlay(username, punisher, reason, ptype, case_no, date_text, avatar_bytes)

        for scale in SCALES:
            data = _encode(overlay, tag_left, durations, scale, step)
            if len(data) <= max_bytes:
                return data
        return _encode(overlay, tag_left, durations, SCALES[-1], step * 2)
