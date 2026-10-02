import io
import math
import os
import re
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

S = 2
W, H = 1080, 600
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

TYPE_STYLE = {
    "WARNING": ((255, 176, 32), 33),
    "MUTE": ((255, 138, 61), 55),
    "TIMEOUT": ((255, 92, 240), 45),
    "KICK": ((255, 77, 109), 78),
    "BAN": ((255, 23, 68), 100),
}

WEIGHT_NAMES = {
    "Orbitron": {500: "Medium", 700: "Bold", 800: "ExtraBold", 900: "Black"},
    "Rajdhani": {500: "Medium", 700: "Bold"},
}
FALLBACK_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
    "arialbd.ttf",
    "Arial Bold.ttf",
]

RES = Image.Resampling


def px(v: float) -> int:
    return int(round(v * S))


@lru_cache(maxsize=None)
def font(family: str, weight: int, size: float) -> ImageFont.FreeTypeFont:
    size_px = px(size)
    name = WEIGHT_NAMES[family][weight]
    static = os.path.join(FONT_DIR, f"{family}-{name}.ttf")
    if os.path.exists(static):
        return ImageFont.truetype(static, size_px)
    variable = os.path.join(FONT_DIR, f"{family}[wght].ttf")
    if os.path.exists(variable):
        f = ImageFont.truetype(variable, size_px)
        try:
            f.set_variation_by_axes([weight])
        except Exception:
            pass
        return f
    for path in FALLBACK_FONTS:
        try:
            return ImageFont.truetype(path, size_px)
        except OSError:
            continue
    return ImageFont.load_default(size_px)


def text_w(text: str, f: ImageFont.FreeTypeFont, sp: float = 0) -> float:
    if not sp:
        return f.getlength(text)
    return sum(f.getlength(c) for c in text) + sp * S * len(text)


def fit(text: str, f, sp: float, max_w: float) -> str:
    text = " ".join((text or "").split()) or "-"
    if text_w(text, f, sp) <= max_w:
        return text
    while text and text_w(text + "...", f, sp) > max_w:
        text = text[:-1]
    return text.rstrip() + "..."


def _spaced(draw: ImageDraw.ImageDraw, x, y, text, f, fill, sp):
    if not sp:
        draw.text((x, y), text, font=f, fill=fill, anchor="ls")
        return
    for ch in text:
        draw.text((x, y), ch, font=f, fill=fill, anchor="ls")
        x += f.getlength(ch) + sp * S


def put(img: Image.Image, layer: Image.Image, x0: int, y0: int):
    sx, sy = max(-x0, 0), max(-y0, 0)
    if sx >= layer.width or sy >= layer.height:
        return
    img.alpha_composite(layer, (max(x0, 0), max(y0, 0)), (sx, sy))


def put_text(img, x, y, text, f, fill, sp=0, glow=None):
    tw = int(text_w(text, f, sp)) + 2
    size = f.size
    blur = glow[1] * S / 2 if glow else 0
    pad = int(blur * 3) + 4
    asc = int(size * 1.2)
    lw, lh = tw + 2 * pad, int(size * 1.7) + 2 * pad
    ox, oy = int(x) - pad, int(y) - asc - pad
    if glow:
        gl = Image.new("RGBA", (lw, lh), tuple(glow[0]) + (0,))
        _spaced(ImageDraw.Draw(gl), pad, asc + pad, text, f, tuple(glow[0]) + (255,), sp)
        put(img, gl.filter(ImageFilter.GaussianBlur(blur)), ox, oy)
    cl = Image.new("RGBA", (lw, lh), tuple(fill[:3]) + (0,))
    _spaced(ImageDraw.Draw(cl), pad, asc + pad, text, f, fill, sp)
    put(img, cl, ox, oy)


def _interp(stops, t):
    t = min(1.0, max(0.0, t))
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        if t <= p1:
            k = 0 if p1 == p0 else min(1.0, max(0.0, (t - p0) / (p1 - p0)))
            a = c0[3] * (1 - k) + c1[3] * k
            if a <= 0:
                return (c1[0], c1[1], c1[2], 0)
            rgb = [(c0[i] * c0[3] * (1 - k) + c1[i] * c1[3] * k) / a for i in range(3)]
            return (int(rgb[0]), int(rgb[1]), int(rgb[2]), int(a))
    return stops[-1][1]


def lin_grad(w, h, stops, angle):
    sw, sh = max(2, w // 16), max(2, h // 16)
    a = math.radians(angle)
    dx, dy = math.sin(a), -math.cos(a)
    length = abs(w * dx) + abs(h * dy) or 1
    data = []
    for j in range(sh):
        y = (j + 0.5) / sh * h - h / 2
        for i in range(sw):
            x = (i + 0.5) / sw * w - w / 2
            data.append(_interp(stops, (x * dx + y * dy) / length + 0.5))
    im = Image.new("RGBA", (sw, sh))
    im.putdata(data)
    return im.resize((w, h), RES.BICUBIC)


def radial(w, h, cx, cy, rx, ry, color):
    r, g, b, a = color
    sw, sh = max(2, w // 12), max(2, h // 12)
    data = []
    for j in range(sh):
        y = (j + 0.5) / sh * h
        for i in range(sw):
            x = (i + 0.5) / sw * w
            d = math.hypot((x - cx) / rx, (y - cy) / ry)
            data.append((r, g, b, int(a * max(0.0, 1 - d))))
    im = Image.new("RGBA", (sw, sh))
    im.putdata(data)
    return im.resize((w, h), RES.BICUBIC)


def hex_mask(w, h):
    m = Image.new("L", (w, h), 0)
    pts = [(0.5, 0), (1, 0.2), (1, 0.8), (0.5, 1), (0, 0.8), (0, 0.2)]
    ImageDraw.Draw(m).polygon([(fx * (w - 1), fy * (h - 1)) for fx, fy in pts], fill=255)
    return m


def _wrap(text, f, max_w, max_lines):
    """Greedy word wrap into at most max_lines lines; the last line gets '...' if text is left over."""
    words = " ".join((text or "").split()).split(" ") or ["-"]
    lines, cur = [], ""
    for wd in words:
        # a single word wider than the line gets broken by characters
        while text_w(wd, f) > max_w:
            k = len(wd)
            while k > 1 and text_w(wd[:k], f) > max_w:
                k -= 1
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(wd[:k])
            wd = wd[k:]
        trial = (cur + " " + wd).strip()
        if text_w(trial, f) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        rest = " ".join(lines[max_lines - 1:])
        lines = lines[:max_lines - 1] + [fit(rest, f, 0, max_w)]
    return lines


def draw_field(img, y, label, value, wrap=False):
    x0, fw, fh = 300, 560, 54
    bg = lin_grad(px(fw), px(fh), [(0, (42, 17, 74, 255)), (1, (255, 255, 255, 5))], 90)
    m = Image.new("L", bg.size, 0)
    ImageDraw.Draw(m).rounded_rectangle(
        (0, 0, bg.width - 1, bg.height - 1), px(14), fill=255, corners=(False, True, True, False)
    )
    bg.putalpha(ImageChops.multiply(bg.getchannel("A"), m))
    put(img, bg, px(x0), px(y))
    put(img, Image.new("RGBA", (px(5), px(fh)), (181, 107, 255, 255)), px(x0), px(y))

    base = px(y + 37)
    lf = font("Orbitron", 800, 14)
    label_w = max(105, text_w(label, lf, 3) / S)
    put_text(img, px(x0 + 25), base, label, lf, (255, 140, 246, 255), sp=3)
    vf = font("Rajdhani", 700, 28)
    max_w = px(fw - 5 - 20 - 20 - 12) - px(label_w)
    vx = px(x0 + 25 + label_w + 12)
    clean = " ".join((value or "").split()) or "-"
    if wrap and text_w(clean, vf) > max_w:
        sf = font("Rajdhani", 700, 21)
        lines = _wrap(clean, sf, max_w, 2)
        if len(lines) == 2:
            put_text(img, vx, px(y + 24), lines[0], sf, (243, 234, 255, 255))
            put_text(img, vx, px(y + 46), lines[1], sf, (243, 234, 255, 255))
            return
        clean = lines[0]
        vf = sf
    put_text(img, vx, base, fit(clean, vf, 0, max_w), vf, (243, 234, 255, 255))


DURATION_RE = re.compile(r"\s*\((for [^)]*)\)\s*$", re.I)


def split_reason(reason):
    """'spam (for 5 minutes)' -> ('spam', '5 minutes'). No duration -> (reason, None)."""
    m = DURATION_RE.search(reason or "")
    if not m:
        return reason, None
    return (reason[:m.start()].strip() or "-"), m.group(1)[4:].strip()


def has_duration(reason):
    return split_reason(reason)[1] is not None


def draw_fields(img, username, punisher, reason):
    """USER / PUNISHER / REASON (wraps to 2 lines) and, for mute/timeout, a DURATION field."""
    text, dur = split_reason(reason)
    draw_field(img, 250, "USER", username)
    draw_field(img, 324, "PUNISHER", punisher)
    draw_field(img, 398, "REASON", text, wrap=True)
    if dur:
        draw_field(img, 472, "DURATION", dur.upper())


def draw_avatar(img, avatar_bytes):
    ax, ay, aw, ah = 44, 100, 220, 250
    border = lin_grad(px(aw), px(ah), [(0, (181, 107, 255, 255)), (1, (255, 92, 240, 255))], 160)
    border.putalpha(hex_mask(*border.size))
    put(img, border, px(ax), px(ay))

    iw, ih = px(aw - 8), px(ah - 8)
    inner = Image.new("RGBA", (iw, ih), (18, 9, 31, 255))
    ok = False
    if avatar_bytes:
        try:
            av = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
            inner.alpha_composite(ImageOps.fit(av, (iw, ih), RES.LANCZOS))
            ok = True
        except Exception:
            ok = False
    if not ok:
        qf = font("Orbitron", 900, 60)
        put_text(inner, (iw - text_w("?", qf)) / 2, ih / 2 + px(22), "?", qf, (91, 58, 148, 255))
    inner.putalpha(hex_mask(iw, ih))
    put(img, inner, px(ax + 4), px(ay + 4))


def draw_bar(img, sev, color=(255, 92, 240)):
    """Severity bar: glossy rounded fill in the punishment's color, soft glow,
    segment ticks, a bright end cap and a small label underneath."""
    bx, by, bw, bh = 44, 402, 220, 12
    color = tuple(color)
    W_, H_ = px(bw), px(bh)
    r = H_ // 2

    fill_w = max(H_, px(bw * sev / 100))
    rmask = Image.new("L", (W_, H_), 0)
    ImageDraw.Draw(rmask).rounded_rectangle((0, 0, W_ - 1, H_ - 1), r, fill=255)
    fmask = Image.new("L", (fill_w, H_), 0)
    ImageDraw.Draw(fmask).rounded_rectangle((0, 0, fill_w - 1, H_ - 1), r, fill=255)

    # glow behind the filled part
    pad = px(10)
    gl = Image.new("L", (W_ + 2 * pad, H_ + 2 * pad), 0)
    gl.paste(fmask, (pad, pad))
    gl = gl.filter(ImageFilter.GaussianBlur(px(5))).point(lambda v: int(v * 0.75))
    glow = Image.new("RGBA", gl.size, color + (0,))
    glow.putalpha(gl)
    put(img, glow, px(bx) - pad, px(by) - pad)

    # track
    bar = Image.new("RGBA", (W_, H_), (30, 16, 52, 255))
    ImageDraw.Draw(bar).rectangle((0, 0, W_, H_ // 3), fill=(20, 9, 36, 255))

    # fill: dark -> bright gradient, glossy top half
    dark = tuple(int(c * 0.45) for c in color)
    fill = lin_grad(fill_w, H_, [(0, dark + (255,)), (1, color + (255,))], 90)
    fill.alpha_composite(Image.new("RGBA", (fill_w, H_ * 2 // 5), (255, 255, 255, 70)), (0, px(1)))
    fill.putalpha(ImageChops.multiply(fill.getchannel("A"), fmask))
    bar.alpha_composite(fill)

    # segment ticks at 25 / 50 / 75 %
    td = ImageDraw.Draw(bar)
    for pct in (25, 50, 75):
        x = px(bw * pct / 100)
        td.rectangle((x - px(0.6), 0, x + px(0.6), H_), fill=(10, 3, 22, 170))

    # bright end cap
    ex = fill_w - r
    cap = Image.new("RGBA", (W_, H_), (0, 0, 0, 0))
    ImageDraw.Draw(cap).ellipse((ex - px(2.2), r - px(2.2), ex + px(2.2), r + px(2.2)), fill=(255, 255, 255, 235))
    bar.alpha_composite(cap.filter(ImageFilter.GaussianBlur(px(0.6))))

    bar.putalpha(ImageChops.multiply(bar.getchannel("A"), rmask))
    put(img, bar, px(bx), px(by))

    # thin outline
    ol = Image.new("RGBA", (W_, H_), (0, 0, 0, 0))
    ImageDraw.Draw(ol).rounded_rectangle((0, 0, W_ - 1, H_ - 1), r, outline=(139, 61, 255, 110), width=px(1))
    put(img, ol, px(bx), px(by))

    # label
    lf = font("Orbitron", 700, 11)
    base = px(by + bh + 17)
    put_text(img, px(bx), base, "SEVERITY", lf, (139, 111, 192, 255), sp=3)
    pct = f"{sev}%"
    put_text(img, px(bx + bw) - text_w(pct, lf, 2), base, pct, lf, color + (255,), sp=2)


def draw_ornament(img, color):
    """Glowing diamond divider under the avatar, in the punishment's color."""
    cx, cy = 44 + 110, 377
    lw, lh = px(220), px(40)
    mid, my = lw // 2, lh // 2
    col = tuple(color)

    glow = Image.new("RGBA", (lw, lh), col + (0,))
    lay = Image.new("RGBA", (lw, lh), col + (0,))

    def diamond(d, x, r, fill):
        d.polygon([(x, my - r), (x + r, my), (x, my + r), (x - r, my)], fill=fill)

    for d in (ImageDraw.Draw(glow), ImageDraw.Draw(lay)):
        # fading lines on both sides
        start, end = px(52), px(106)
        for off in range(start, end):
            a = int(255 * (1 - (off - start) / (end - start)))
            for sx in (-1, 1):
                x = mid + sx * off
                d.rectangle((x, my - px(0.75), x, my + px(0.75)), fill=col + (a,))
        # small side diamonds
        for sx in (-1, 1):
            diamond(d, mid + sx * px(24), px(4), col + (255,))
            diamond(d, mid + sx * px(38), px(2.5), col + (170,))
        # big center diamond
        diamond(d, mid, px(9), col + (255,))

    diamond(ImageDraw.Draw(lay), mid, px(3.5), (255, 255, 255, 255))
    put(img, glow.filter(ImageFilter.GaussianBlur(px(5))), px(cx) - lw // 2, px(cy) - lh // 2)
    put(img, lay, px(cx) - lw // 2, px(cy) - lh // 2)


def draw_stamp(img, text, color):
    f = font("Orbitron", 900, 44)
    tw = text_w(text, f, 6) / S
    bw, bh, mg = tw + 44 + 12, 44 * 1.25 + 16 + 12, 40
    lw, lh = px(bw + 2 * mg), px(bh + 2 * mg)
    box = (px(mg), px(mg), px(mg + bw) - 1, px(mg + bh) - 1)

    sh = Image.new("RGBA", (lw, lh), tuple(color) + (0,))
    a = Image.new("L", (lw, lh), 0)
    ImageDraw.Draw(a).rounded_rectangle(box, px(10), fill=102)
    a = a.filter(ImageFilter.GaussianBlur(px(18) / 2))
    hole = Image.new("L", (lw, lh), 255)
    ImageDraw.Draw(hole).rounded_rectangle(box, px(10), fill=0)
    sh.putalpha(ImageChops.multiply(a, hole))

    layer = Image.new("RGBA", (lw, lh), tuple(color) + (0,))
    d = ImageDraw.Draw(layer)
    col = tuple(color) + (255,)
    d.rounded_rectangle(box, px(10), outline=col, width=px(2))
    inner = (box[0] + px(4), box[1] + px(4), box[2] - px(4), box[3] - px(4))
    d.rounded_rectangle(inner, px(6), outline=col, width=px(2))
    put_text(layer, px(mg + 6 + 22), px(mg + 6 + 8 + 41), text, f, col, sp=6, glow=(color, 14))

    out = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
    put(out, sh, 0, 0)
    put(out, layer, 0, 0)
    out.putalpha(out.getchannel("A").point(lambda v: int(v * 0.92)))
    rot = out.rotate(12, resample=RES.BICUBIC, expand=True)
    cx, cy = W - 50 - bw / 2, 112 + bh / 2
    put(img, rot, px(cx) - rot.width // 2, px(cy) - rot.height // 2)


def render_card(username, punisher, reason, ptype, case_no, date_text, avatar_bytes=None) -> bytes:
    ptype = ptype.upper()
    color, sev = TYPE_STYLE[ptype]
    w, h = W * S, H * S

    card_mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(card_mask).rounded_rectangle((0, 0, w - 1, h - 1), px(22), fill=255)

    img = lin_grad(w, h, [(0, (13, 4, 32, 255)), (0.6, (26, 7, 54, 255)), (1, (10, 3, 22, 255))], 135)
    put(img, radial(w, h, 0.05 * w, h, px(500), px(400), (192, 27, 255, 0x44)), 0, 0)
    put(img, radial(w, h, 0.85 * w, 0.1 * h, px(600), px(400), (91, 27, 189, 0x55)), 0, 0)

    pad = px(80)
    big = Image.new("L", (w + 2 * pad, h + 2 * pad), 0)
    big.paste(card_mask, (pad, pad))
    big = big.filter(ImageFilter.GaussianBlur(px(40) / 2)).crop((pad, pad, pad + w, pad + h))
    glow_a = ImageChops.invert(big).point(lambda v: int(v * 0x88 / 255))
    glow = Image.new("RGBA", (w, h), (122, 44, 255, 0))
    glow.putalpha(glow_a)
    put(img, glow, 0, 0)

    lines = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lines)
    for y in range(0, h, px(5)):
        ld.rectangle((0, y, w, y + px(2) - 1), fill=(255, 255, 255, 6))
    put(img, lines, 0, 0)

    wf = font("Orbitron", 900, 300)
    put_text(img, px(W + 20) - text_w("ELT", wf, -10), px(H - 12), "ELT", wf, (139, 61, 255, 18), sp=-10)

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

    border = Image.new("RGBA", (w, h), (139, 61, 255, 0))
    ba = Image.new("L", (w, h), 0)
    ImageDraw.Draw(ba).rounded_rectangle((0, 0, w - 1, h - 1), px(22), outline=255, width=px(2))
    border.putalpha(ba)
    put(img, border, 0, 0)
    img.putalpha(card_mask)
    img = img.resize((W, H), RES.LANCZOS)

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
