"""
Animated punishment card (Pillow only - no Discord code in here).

render_card_gif(...) returns GIF bytes: the same ELT punishment card as
punishment_card.py, drawn on top of an animated background (card_bg.gif),
plus a light-sweep and a blinking "recording" dot.

It reuses the drawing helpers from punishment_card.py, so keep both files in
the same folder, together with:
    card_bg.gif        the animated background
    fonts/             Orbitron + Rajdhani (see punishment_card.py)
"""

import io
import os
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageSequence

from punishment_card import (
    FONT_DIR, H, RES, S, TYPE_STYLE, W, WEIGHT_NAMES,
    draw_avatar, draw_bar, draw_field, draw_ornament, draw_stamp,
    font, lin_grad, put, put_text, px, text_w,
)

HERE = os.path.dirname(os.path.abspath(__file__))
BG_PATH = os.environ.get("CARD_BG_PATH") or os.path.join(HERE, "card_bg.gif")

MAX_BYTES = 7_500_000            # stay well under Discord's 10 MB upload limit
SCALES = (1.0, 0.85, 0.7, 0.6)   # if the GIF is too big, retry smaller
BG_BRIGHTNESS = 0.62             # 1.0 = untouched background, lower = darker (more readable text)
TRANSPARENT = 255                # palette slot reserved for the rounded corners


# ------------------------------ fonts check ------------------------------
def _check_fonts():
    """Prints a loud warning if the fonts folder didn't make it to the server.
    Without it the card silently falls back to a plain font and looks wrong."""
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
def clean_for_card(text: str) -> str:
    """Removes characters the card font can't draw (they'd show up as empty boxes),
    e.g. emoji or Arabic in a display name. Returns '' if nothing drawable is left,
    so the caller can fall back to the plain username."""
    text = " ".join((text or "").split())
    if not text:
        return ""
    try:
        f = font("Rajdhani", 700, 28)

        def glyph(ch):  # what the font actually draws for this character
            im = Image.new("L", (80, 90), 0)
            ImageDraw.Draw(im).text((10, 10), ch, font=f, fill=255)
            return im.tobytes()

        notdef = glyph("\U0010FFFF")          # the font's "missing character" box
        kept = [ch for ch in text if ch == " " or glyph(ch) != notdef]
    except Exception:
        return text
    return " ".join("".join(kept).split())


# ------------------------------ background ------------------------------
@lru_cache(maxsize=1)
def _load_bg(path: str):
    im = Image.open(path)
    frames, durations = [], []
    for fr in ImageSequence.Iterator(im):
        durations.append(max(20, fr.info.get("duration", 100) or 100))
        frames.append(fr.convert("RGB"))
    return tuple(frames), tuple(durations)


def _prep_bg(frame: Image.Image, size) -> Image.Image:
    f = ImageOps.fit(frame, size, RES.LANCZOS)
    table = [int(i * BG_BRIGHTNESS) for i in range(256)]
    return f.point(table * 3).convert("RGBA")


# ------------------------------ static overlay ------------------------------
def _panel(img, x, y, w, h, radius, fill, outline=None):
    layer = Image.new("RGBA", (px(w), px(h)), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle((0, 0, layer.width - 1, layer.height - 1), px(radius), fill=fill,
                        outline=outline, width=px(1.5) if outline else 0)
    put(img, layer, px(x), px(y))


def _brackets(img, inset=16, length=34, thick=3, color=(181, 107, 255, 220)):
    """Little sci-fi corner brackets, to match the HUD look of the background."""
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
    """Everything on the card except the moving background. Drawn once, at 2x, then shrunk."""
    color, sev = TYPE_STYLE[ptype]
    w, h = W * S, H * S
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))

    # dark purple tint so the text stays readable over the moving background
    put(img, lin_grad(w, h, [(0, (12, 3, 28, 165)), (0.55, (20, 6, 44, 120)), (1, (9, 2, 20, 165))], 135), 0, 0)

    # soft glow around the inside edge
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), px(22), fill=255)
    pad = px(80)
    big = Image.new("L", (w + 2 * pad, h + 2 * pad), 0)
    big.paste(mask, (pad, pad))
    big = big.filter(ImageFilter.GaussianBlur(px(40) / 2)).crop((pad, pad, pad + w, pad + h))
    edge = Image.new("RGBA", (w, h), (122, 44, 255, 0))
    edge.putalpha(ImageChops.invert(big).point(lambda v: int(v * 0x88 / 255)))
    put(img, edge, 0, 0)

    # scanlines
    lines = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lines)
    for y in range(0, h, px(5)):
        ld.rectangle((0, y, w, y + px(2) - 1), fill=(255, 255, 255, 6))
    put(img, lines, 0, 0)

    # glass panels behind the avatar column and the three fields
    _panel(img, 28, 84, 250, 358, 18, (10, 3, 22, 150), (139, 61, 255, 90))
    _panel(img, 288, 238, 590, 228, 18, (10, 3, 22, 140), (139, 61, 255, 70))

    # top bar
    tf = font("Orbitron", 700, 20)
    put_text(img, px(40), px(49), "ELT COMMUNITY", tf, (255, 255, 255, 255), sp=6, glow=((181, 107, 255), 14))
    tag = "// PUNISHMENT LOG"
    put_text(img, px(W - 40) - text_w(tag, tf, 6), px(49), tag, tf, (199, 163, 255, 255), sp=6)

    # title
    put_text(img, px(300), px(132), "NEW", font("Orbitron", 900, 62), (255, 255, 255, 255), sp=4,
             glow=((181, 107, 255), 32))
    put_text(img, px(300), px(172), "PUNISHMENT", font("Orbitron", 900, 30), (255, 92, 240, 255), sp=14,
             glow=((255, 92, 240), 14))

    # avatar, ornament, severity bar
    draw_avatar(img, avatar_bytes)
    draw_ornament(img, color)
    draw_bar(img, sev, color)

    # fields
    draw_field(img, 250, "USER", username)
    draw_field(img, 324, "PUNISHER", punisher)
    draw_field(img, 398, "REASON", reason)

    draw_stamp(img, ptype, color)

    # date + signature
    put_text(img, px(300), px(H - 40), date_text, font("Orbitron", 700, 26), (199, 163, 255, 255), sp=3)
    sf, ef = font("Orbitron", 800, 14), font("Orbitron", 800, 26)
    put_text(img, px(W - 40) - text_w("ISSUED BY", sf, 5), px(H - 66), "ISSUED BY", sf, (139, 111, 192, 255), sp=5)
    put_text(img, px(W - 40) - text_w("ELITE", ef, 0), px(H - 38), "ELITE", ef, (255, 255, 255, 255),
             glow=((181, 107, 255), 12))

    _brackets(img)

    # border
    ring = Image.new("RGBA", (w, h), (139, 61, 255, 0))
    ba = Image.new("L", (w, h), 0)
    ImageDraw.Draw(ba).rounded_rectangle((0, 0, w - 1, h - 1), px(22), outline=255, width=px(2))
    ring.putalpha(ba)
    put(img, ring, 0, 0)

    tag_left = W - 40 - text_w(tag, tf, 6) / S          # where the tag text starts (1x px)
    return img.resize((W, H), RES.LANCZOS), tag_left


def _blink_dot(color=(255, 92, 240)):
    """Small glowing dot shown on every other stretch of frames."""
    size = 40
    dot = Image.new("RGBA", (size, size), color + (0,))
    ImageDraw.Draw(dot).ellipse((size / 2 - 6, size / 2 - 6, size / 2 + 6, size / 2 + 6), fill=color + (255,))
    glow = dot.filter(ImageFilter.GaussianBlur(6))
    glow.alpha_composite(dot)
    return glow


def _sweep_band(fw, fh):
    """A soft slanted streak of light that travels across the card."""
    bw = int(fw * 0.20)
    band = lin_grad(bw, fh, [(0, (210, 160, 255, 0)), (0.5, (225, 190, 255, 44)), (1, (210, 160, 255, 0))], 90)
    shear = 0.35
    ow = bw + int(fh * shear)
    return band.transform((ow, fh), Image.AFFINE, (1, shear, -fh * shear, 0, 1, 0), resample=Image.BILINEAR)


# ------------------------------ encoding ------------------------------
def _encode(overlay, tag_left, bg_frames, durations, scale, step):
    fw, fh = int(round(W * scale)), int(round(H * scale))
    if scale != 1.0:
        overlay = overlay.resize((fw, fh), RES.LANCZOS)

    idx = list(range(0, len(bg_frames), step))
    n = len(idx)
    band = _sweep_band(fw, fh)
    dot = _blink_dot()
    if scale != 1.0:
        dot = dot.resize((int(dot.width * scale), int(dot.height * scale)), RES.LANCZOS)
    dot_xy = (int((tag_left - 16) * scale) - dot.width // 2, int(42 * scale) - dot.height // 2)

    corner = Image.new("L", (W * 2, H * 2), 0)
    ImageDraw.Draw(corner).rounded_rectangle((0, 0, W * 2 - 1, H * 2 - 1), 44, fill=255)
    corner = corner.resize((fw, fh), RES.LANCZOS)
    hole = corner.point(lambda v: 255 if v < 128 else 0)        # 255 = rounded-corner area -> transparent

    def compose(k):
        base = _prep_bg(bg_frames[idx[k]], (fw, fh))
        base.alpha_composite(overlay)
        t = k / max(1, n)
        if t < 0.75:                                            # one sweep per loop, then a pause
            x = int(-band.width + (fw + band.width) * (t / 0.75))
            put(base, band, x, 0)
        if (k * 4 // n) % 2 == 0:                               # dot blinks twice per loop
            put(base, dot, *dot_xy)
        return base.convert("RGB")

    # one shared palette for the whole GIF (per-frame palettes make colours flicker)
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

    out_frames = []
    for k in range(n):
        q = compose(k).quantize(palette=pal, dither=0)
        q.paste(TRANSPARENT, mask=hole)
        out_frames.append(q)

    durs = [sum(durations[idx[k]:idx[k] + step]) for k in range(n)]
    buf = io.BytesIO()
    out_frames[0].save(
        buf, format="GIF", save_all=True, append_images=out_frames[1:],
        duration=durs, loop=0, transparency=TRANSPARENT, disposal=2, optimize=False,
    )
    return buf.getvalue()


def render_card_gif(username, punisher, reason, ptype, case_no, date_text,
                    avatar_bytes=None, max_bytes=MAX_BYTES) -> bytes:
    """Same arguments as punishment_card.render_card, but returns an animated GIF.
    Raises FileNotFoundError if card_bg.gif is missing (the bot then falls back to the PNG card)."""
    ptype = ptype.upper()
    username, punisher = clean_for_card(username) or "Unknown", clean_for_card(punisher) or "Unknown"
    reason = clean_for_card(reason) or "-"

    bg_frames, durations = _load_bg(BG_PATH)
    overlay, tag_left = _build_overlay(username, punisher, reason, ptype, case_no, date_text, avatar_bytes)

    data = b""
    for scale in SCALES:
        data = _encode(overlay, tag_left, bg_frames, durations, scale, 1)
        if len(data) <= max_bytes:
            return data
    # still too big: keep the smallest size but drop every other frame
    return _encode(overlay, tag_left, bg_frames, durations, SCALES[-1], 2)
