"""Clock-on-profile-photo renderer (Pillow).

render(raw_base_photo_bytes | None, style, color, now) -> JPEG bytes (800x800)

17 designs x 7 colors. The base photo is the user's own profile picture (or a gradient if there is none);
every design is drawn ON TOP of it, so the photo stays visible.
"""
import functools
import io
import math
import os
from datetime import datetime

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

S = 800
_FD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")
_SYS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf", "Arial.ttf"]

# key -> (Persian name, dot emoji for buttons, RGB)
COLORS = {
    "red": ("قرمز", "🔴", (255, 38, 62)),
    "blue": ("آبی", "🔵", (45, 135, 255)),
    "green": ("سبز", "🟢", (40, 232, 120)),
    "purple": ("بنفش", "🟣", (176, 84, 255)),
    "gold": ("طلایی", "🟡", (255, 196, 40)),
    "pink": ("صورتی", "🩷", (255, 92, 172)),
    "cyan": ("فیروزه‌ای", "🩵", (28, 226, 236)),
}
COLOR_ORDER = list(COLORS)

# key -> Persian name
STYLES = [
    ("neon", "نئون"), ("stack", "دو طبقه"), ("ring", "حلقه"), ("analog", "آنالوگ"),
    ("glass", "شیشه‌ای"), ("hud", "هولوگرام"), ("capsule", "کپسول"), ("minimal", "مینیمال"),
]


# ------------------------------------------------------------------ small helpers
@functools.lru_cache(maxsize=128)
def _font(name, size):
    for p in (os.path.join(_FD, name), *_SYS):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


@functools.lru_cache(maxsize=512)
def _fit(name, text, maxw, start):
    size = start
    while size > 20:
        f = _font(name, size)
        if f.getlength(text) <= maxw:
            return f
        size -= 6
    return _font(name, 20)


def tint(c, k):        # mix with white
    return tuple(int(v + (255 - v) * k) for v in c)


def shade(c, k):       # mix with black
    return tuple(int(v * (1 - k)) for v in c)


def _layer():
    return Image.new("RGBA", (S, S), (0, 0, 0, 0))


def _over(img, layer):
    return Image.alpha_composite(img, layer)


def _dim(img, a, vignette=True):
    """Darken so light/dark photos both keep the clock readable."""
    out = _over(img, Image.new("RGBA", (S, S), (0, 0, 0, int(255 * a))))
    if vignette:
        v = Image.new("L", (S, S), 0)
        d = ImageDraw.Draw(v)
        d.ellipse((-S * 0.15, -S * 0.15, S * 1.15, S * 1.15), fill=255)
        v = v.filter(ImageFilter.GaussianBlur(120))
        edge = Image.new("RGBA", (S, S), (0, 0, 0, 120))
        edge.putalpha(v.point(lambda p: int(120 * (1 - p / 255))))
        out = _over(out, edge)
    return out


def _mask_text(text, pos, font, anchor="mm"):
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).text(pos, text, font=font, fill=255, anchor=anchor)
    return m


def _glow_from_mask(img, mask, col, radius=16, strength=1.7, passes=2):
    g = mask.filter(ImageFilter.GaussianBlur(radius)).point(lambda p: min(255, int(p * strength)))
    for _ in range(passes):
        layer = Image.new("RGBA", (S, S), col + (255,))
        layer.putalpha(g)
        img = _over(img, layer)
    return img


def glow_text(img, text, pos, font, col, anchor="mm", radius=16, core=None, strength=1.7):
    m = _mask_text(text, pos, font, anchor)
    img = _glow_from_mask(img, m, col, radius, strength)
    layer = _layer()
    ImageDraw.Draw(layer).text(pos, text, font=font, fill=(core or tint(col, 0.82)) + (255,), anchor=anchor)
    return _over(img, layer)


def plain_text(img, text, pos, font, fill, anchor="mm", shadow=True):
    layer = _layer()
    d = ImageDraw.Draw(layer)
    if shadow:
        sh = _mask_text(text, (pos[0] + 3, pos[1] + 4), font, anchor).filter(ImageFilter.GaussianBlur(5))
        sl = Image.new("RGBA", (S, S), (0, 0, 0, 255))
        sl.putalpha(sh.point(lambda p: int(p * 0.7)))
        img = _over(img, sl)
    d.text(pos, text, font=font, fill=fill, anchor=anchor)
    return _over(img, layer)


class _SS:
    """2x supersampled drawing layer (smooth circles / arcs / hands)."""

    def __init__(self):
        self.im = Image.new("RGBA", (S * 2, S * 2), (0, 0, 0, 0))
        self.d = ImageDraw.Draw(self.im)

    def xy(self, *v):
        return [int(x * 2) for x in v]

    def done(self):
        return self.im.resize((S, S), Image.LANCZOS)


def _round_mask(box, r):
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).rounded_rectangle(box, radius=r, fill=255)
    return m


def _polar(cx, cy, r, deg):
    a = math.radians(deg - 90)
    return cx + r * math.cos(a), cy + r * math.sin(a)


# ------------------------------------------------------------------ base photo
def make_base(raw, col):
    if raw:
        try:
            im = Image.open(io.BytesIO(raw))
            im = ImageOps.exif_transpose(im).convert("RGB")
            return ImageOps.fit(im, (S, S), Image.LANCZOS).convert("RGBA")
        except Exception:                                   # noqa: BLE001
            pass
    im = Image.new("RGB", (S, S))
    d = ImageDraw.Draw(im)
    top, bot = shade(col, 0.55), (8, 8, 14)
    for y in range(S):
        t = y / S
        d.line([(0, y), (S, y)], fill=tuple(int(top[i] * (1 - t) + bot[i] * t) for i in range(3)))
    return im.convert("RGBA")


def normalize_base(raw, size=640):
    """Square-crop + shrink an uploaded photo so it is cheap to store (JPEG bytes)."""
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    im = ImageOps.fit(im, (size, size), Image.LANCZOS)
    bio = io.BytesIO()
    im.save(bio, "JPEG", quality=86)
    return bio.getvalue()


# ------------------------------------------------------------------ designs
def _neon(img, c, hm, date, now):
    img = _dim(img, 0.55)
    f = _fit("Poppins-Bold.ttf", hm, S * 0.84, 230)
    for y in (S * 0.31, S * 0.69):                           # neon tubes
        m = Image.new("L", (S, S), 0)
        ImageDraw.Draw(m).rounded_rectangle((S * 0.2, y - 4, S * 0.8, y + 4), radius=4, fill=255)
        img = _glow_from_mask(img, m, c, 10, 1.6)
        lay = _layer()
        ImageDraw.Draw(lay).rounded_rectangle((S * 0.2, y - 3, S * 0.8, y + 3), radius=3, fill=tint(c, 0.7) + (255,))
        img = _over(img, lay)
    img = glow_text(img, hm, (S / 2, S / 2), f, c, radius=22)
    return plain_text(img, date, (S / 2, S * 0.81), _font("Poppins-Medium.ttf", 44), tint(c, 0.6) + (255,))


def _stack(img, c, hm, date, now):
    img = _dim(img, 0.5)
    hh, mm = hm.split(":")
    f = _font("Poppins-Bold.ttf", 330)
    img = plain_text(img, hh, (S / 2, S * 0.34), f, (255, 255, 255, 255))
    img = glow_text(img, mm, (S / 2, S * 0.68), f, c, radius=20, core=tint(c, 0.35))
    lay = _layer()
    ImageDraw.Draw(lay).rounded_rectangle((S * 0.30, S * 0.5 - 5, S * 0.70, S * 0.5 + 5), radius=5, fill=c + (255,))
    img = _over(img, lay)
    return plain_text(img, date, (S / 2, S * 0.92), _font("Poppins-Medium.ttf", 38), (255, 255, 255, 220))


def _ring(img, c, hm, date, now):
    img = _dim(img, 0.5)
    cx = cy = S / 2
    R, W = 292, 28
    ss = _SS()
    ss.d.ellipse(ss.xy(cx - R, cy - R, cx + R, cy + R), outline=(255, 255, 255, 55), width=W * 2)
    prog = max(now.minute / 60, 0.012)
    ss.d.arc(ss.xy(cx - R, cy - R, cx + R, cy + R), -90, -90 + 360 * prog, fill=c + (255,), width=W * 2)
    ex, ey = _polar(cx, cy, R, 360 * prog)                   # round cap at the end
    ss.d.ellipse(ss.xy(ex - W / 2, ey - W / 2, ex + W / 2, ey + W / 2), fill=tint(c, 0.5) + (255,))
    for i in range(60):                                      # tick dots
        px, py = _polar(cx, cy, R + 52, i * 6)
        r = 5 if i % 5 == 0 else 2.5
        on = i <= now.minute
        ss.d.ellipse(ss.xy(px - r, py - r, px + r, py + r), fill=(c + (255,)) if on else (255, 255, 255, 90))
    ring = ss.done()
    m = ring.split()[3]
    img = _glow_from_mask(img, m, c, 14, 1.2, 1)
    img = _over(img, ring)
    f = _fit("Poppins-Bold.ttf", hm, 370, 150)
    img = plain_text(img, hm, (cx, cy - 12), f, (255, 255, 255, 255))
    return plain_text(img, date, (cx, cy + 92), _font("Poppins-Medium.ttf", 34), tint(c, 0.55) + (255,))


def _analog(img, c, hm, date, now):
    img = _dim(img, 0.3)
    cx = cy = S / 2
    R = 336
    ss = _SS()
    ss.d.ellipse(ss.xy(cx - R, cy - R, cx + R, cy + R), fill=(8, 8, 14, 170))
    ss.d.ellipse(ss.xy(cx - R, cy - R, cx + R, cy + R), outline=c + (255,), width=10)
    for i in range(60):
        big = i % 5 == 0
        a, b = _polar(cx, cy, R - (46 if big else 28), i * 6), _polar(cx, cy, R - 14, i * 6)
        ss.d.line(ss.xy(*a, *b), fill=(255, 255, 255, 255) if big else (255, 255, 255, 120), width=(8 if big else 3) * 2 // 2 * 2)
    face = ss.done()
    img = _glow_from_mask(img, face.split()[3].point(lambda p: 255 if p > 250 else 0), c, 18, 0.9, 1)
    img = _over(img, face)
    fn = _font("Poppins-Medium.ttf", 48)
    for n, deg in ((12, 0), (3, 90), (6, 180), (9, 270)):
        x, y = _polar(cx, cy, R - 92, deg)
        img = plain_text(img, str(n), (x, y), fn, tint(c, 0.7) + (255,), shadow=False)
    hands = _SS()
    ha = (now.hour % 12 + now.minute / 60) * 30
    ma = now.minute * 6
    for ang, ln, w, col in ((ha, 170, 22, (255, 255, 255, 255)), (ma, 255, 14, c + (255,))):
        x, y = _polar(cx, cy, ln, ang)
        hands.d.line(hands.xy(cx, cy, x, y), fill=col, width=w * 2)
        hands.d.ellipse(hands.xy(x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=col)
    hands.d.ellipse(hands.xy(cx - 20, cy - 20, cx + 20, cy + 20), fill=c + (255,))
    hands.d.ellipse(hands.xy(cx - 8, cy - 8, cx + 8, cy + 8), fill=(255, 255, 255, 255))
    hl = hands.done()
    sh = Image.new("RGBA", (S, S), (0, 0, 0, 255))
    sh.putalpha(hl.split()[3].filter(ImageFilter.GaussianBlur(6)).point(lambda p: int(p * 0.6)))
    img = _over(_over(img, sh), hl)
    return plain_text(img, hm, (cx, cy + 150), _font("Poppins-Bold.ttf", 54), (255, 255, 255, 235))


def _glass(img, c, hm, date, now):
    img = _dim(img, 0.12, vignette=False)
    box = (56, 462, S - 56, S - 64)
    m = _round_mask(box, 54)
    blur = img.filter(ImageFilter.GaussianBlur(30))
    img.paste(blur, (0, 0), m)
    lay = _layer()
    d = ImageDraw.Draw(lay)
    d.rounded_rectangle(box, radius=54, fill=(255, 255, 255, 52), outline=(255, 255, 255, 120), width=3)
    d.rounded_rectangle((box[0] + 3, box[1] + 3, box[2] - 3, box[1] + 90), radius=48, fill=(255, 255, 255, 18))
    img = _over(img, lay)
    f = _fit("Poppins-Bold.ttf", hm, 560, 210)
    img = plain_text(img, hm, (S / 2, 570), f, (255, 255, 255, 255))
    bar = _layer()
    ImageDraw.Draw(bar).rounded_rectangle((S / 2 - 90, 662, S / 2 + 90, 672), radius=5, fill=c + (255,))
    img = _glow_from_mask(img, bar.split()[3], c, 8, 1.2, 1)
    img = _over(img, bar)
    return plain_text(img, date, (S / 2, 708), _font("Poppins-Medium.ttf", 40), (255, 255, 255, 235))


def _hud(img, c, hm, date, now):
    img = _dim(img, 0.4)
    ss = _SS()
    ins, ln, w = 52, 120, 8
    for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):      # corner brackets
        x0 = ins if sx == 1 else S - ins
        y0 = ins if sy == 1 else S - ins
        ss.d.line(ss.xy(x0, y0, x0 + sx * ln, y0), fill=c + (255,), width=w * 2)
        ss.d.line(ss.xy(x0, y0, x0, y0 + sy * ln), fill=c + (255,), width=w * 2)
    cx = cy = S / 2
    R = 330
    for k in range(4):                                       # broken crosshair circle
        ss.d.arc(ss.xy(cx - R, cy - R, cx + R, cy + R), k * 90 + 14, k * 90 + 76, fill=c + (150,), width=4)
    for deg in (0, 90, 180, 270):
        a, b = _polar(cx, cy, R - 4, deg), _polar(cx, cy, R + 24, deg)
        ss.d.line(ss.xy(*a, *b), fill=c + (255,), width=6)
    hud = ss.done()
    img = _glow_from_mask(img, hud.split()[3], c, 10, 1.4, 1)
    img = _over(img, hud)
    f = _fit("DejaVuSansMono-Bold.ttf", hm, 500, 190)
    img = glow_text(img, hm, (cx, cy), f, c, radius=14, core=tint(c, 0.7))
    lay = _layer()
    d = ImageDraw.Draw(lay)
    for y in range(0, S, 6):                                 # scanlines
        d.line([(0, y), (S, y)], fill=(0, 0, 0, 34))
    d.ellipse((ins + 22, ins + 26, ins + 38, ins + 42), fill=c + (255,))
    img = _over(img, lay)
    fm = _font("DejaVuSansMono-Bold.ttf", 32)
    img = plain_text(img, "LIVE", (ins + 52, ins + 34), fm, tint(c, 0.6) + (255,), anchor="lm", shadow=False)
    return plain_text(img, f"TEHRAN  {date}", (cx, S - ins - 36), fm, tint(c, 0.6) + (255,), shadow=False)


def _capsule(img, c, hm, date, now):
    img = _dim(img, 0.1, vignette=False)
    box = (S / 2 - 270, 560, S / 2 + 270, 706)
    sh = Image.new("RGBA", (S, S), (0, 0, 0, 255))
    sh.putalpha(_round_mask((box[0] + 6, box[1] + 14, box[2] + 6, box[3] + 14), 73).filter(ImageFilter.GaussianBlur(18)).point(lambda p: int(p * 0.65)))
    img = _over(img, sh)
    grad = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    c1, c2 = tint(c, 0.12), shade(c, 0.35)
    for x in range(int(box[0]), int(box[2])):
        t = (x - box[0]) / (box[2] - box[0])
        gd.line([(x, box[1]), (x, box[3])], fill=tuple(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3)) + (255,))
    grad.putalpha(_round_mask(box, 73))
    img = _over(img, grad)
    hi = _layer()
    ImageDraw.Draw(hi).rounded_rectangle((box[0] + 8, box[1] + 6, box[2] - 8, box[1] + 62), radius=30, fill=(255, 255, 255, 38))
    img = _over(img, hi)
    ss = _SS()                                               # little clock icon
    ix, iy, ir = box[0] + 82, (box[1] + box[3]) / 2, 36
    ss.d.ellipse(ss.xy(ix - ir, iy - ir, ix + ir, iy + ir), outline=(255, 255, 255, 255), width=10)
    ss.d.line(ss.xy(ix, iy, ix, iy - 22), fill=(255, 255, 255, 255), width=10)
    ss.d.line(ss.xy(ix, iy, ix + 16, iy + 8), fill=(255, 255, 255, 255), width=10)
    img = _over(img, ss.done())
    f = _fit("Poppins-Bold.ttf", hm, 340, 118)
    img = plain_text(img, hm, (S / 2 + 56, (box[1] + box[3]) / 2 - 2), f, (255, 255, 255, 255), shadow=False)
    return plain_text(img, date, (S / 2, 752), _font("Poppins-Medium.ttf", 36), (255, 255, 255, 235))


def _minimal(img, c, hm, date, now):
    g = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(g)
    for y in range(int(S * 0.42), S):
        t = (y - S * 0.42) / (S * 0.58)
        gd.line([(0, y), (S, y)], fill=(0, 0, 0, int(215 * t ** 1.4)))
    img = _over(img, g)
    f = _fit("Poppins-Light.ttf", hm, S - 150, 230)
    img = plain_text(img, hm, (70, 676), f, (255, 255, 255, 255), anchor="ls")
    bar = _layer()
    ImageDraw.Draw(bar).rounded_rectangle((72, 704, 72 + 150, 716), radius=6, fill=c + (255,))
    img = _glow_from_mask(img, bar.split()[3], c, 7, 1.1, 1)
    img = _over(img, bar)
    return plain_text(img, date, (70, 758), _font("Poppins-Medium.ttf", 38), (255, 255, 255, 215), anchor="ls")


# ------------------------------------------------------------------ v18: extra designs
import colorsys
import random


def _hue(c, shift, s=None, v=None):
    h, sat, val = colorsys.rgb_to_hsv(*[x / 255 for x in c])
    r, g, b = colorsys.hsv_to_rgb((h + shift) % 1.0, sat if s is None else s, val if v is None else v)
    return int(r * 255), int(g * 255), int(b * 255)


def _vgrad(w, h, top, bot):
    g = Image.linear_gradient("L").resize((w, h))
    a = Image.new("RGB", (w, h), top)
    b = Image.new("RGB", (w, h), bot)
    return Image.composite(b, a, g)


def _aurora(img, c, hm, date, now):
    img = _dim(img, 0.5)
    lay = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    cols = [c, _hue(c, 0.12), _hue(c, -0.14), _hue(c, 0.3)]
    rnd = random.Random(now.hour * 60 + now.minute)
    for i, col in enumerate(cols):
        cx, cy = rnd.randint(120, 680), rnd.randint(120, 680)
        rx, ry = rnd.randint(190, 330), rnd.randint(110, 220)
        d.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=col + (120,))
    lay = lay.filter(ImageFilter.GaussianBlur(70))
    img = _over(img, lay)
    f = _fit("Poppins-Light.ttf", hm, S * 0.86, 300)
    img = plain_text(img, hm, (S / 2, S / 2 - 10), f, (255, 255, 255, 255))
    return plain_text(img, date, (S / 2, S / 2 + 165), _font("Poppins-Medium.ttf", 42), (255, 255, 255, 225))


def _glitch(img, c, hm, date, now):
    img = _dim(img, 0.55)
    f = _fit("Poppins-Bold.ttf", hm, S * 0.86, 250)
    c2 = _hue(c, 0.5, 0.9, 1.0)
    rnd = random.Random(now.minute * 7 + now.hour)
    base = Image.new("L", (S, S), 0)
    ImageDraw.Draw(base).text((S / 2, S / 2), hm, font=f, fill=255, anchor="mm")
    for col, dx in ((c, -9), (c2, 9)):
        m = Image.new("L", (S, S), 0)
        m.paste(base, (dx, 0))
        lay = Image.new("RGBA", (S, S), col + (255,))
        lay.putalpha(m.point(lambda p: int(p * 0.85)))
        img = _over(img, lay)
    white = Image.new("RGBA", (S, S), (255, 255, 255, 255))
    sl = base.copy()
    for _ in range(7):                                        # glitch slices
        y = rnd.randint(250, 540)
        h = rnd.randint(8, 30)
        sx = rnd.randint(-40, 40)
        strip = sl.crop((0, y, S, y + h))
        sl.paste(0, (0, y, S, y + h))
        sl.paste(strip, (sx, y))
    white.putalpha(sl)
    img = _over(img, white)
    lay = _layer()
    d = ImageDraw.Draw(lay)
    for y in range(0, S, 5):
        d.line([(0, y), (S, y)], fill=(0, 0, 0, 40))
    img = _over(img, lay)
    fm = _font("DejaVuSansMono-Bold.ttf", 34)
    img = plain_text(img, f"> {date}_", (S / 2, S / 2 + 190), fm, tint(c, 0.6) + (255,), shadow=False)
    return img


_SEG = {"0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg", "5": "acdfg",
        "6": "acdefg", "7": "abc", "8": "abcdefg", "9": "abcdfg"}


def _seg_digit(d, ch, x, y, w, h, t, on, off):
    g = 5
    m = h / 2
    rects = {
        "a": (x + t + g, y, x + w - t - g, y + t),
        "g": (x + t + g, y + m - t / 2, x + w - t - g, y + m + t / 2),
        "d": (x + t + g, y + h - t, x + w - t - g, y + h),
        "f": (x, y + t + g, x + t, y + m - g),
        "b": (x + w - t, y + t + g, x + w, y + m - g),
        "e": (x, y + m + g, x + t, y + h - t - g),
        "c": (x + w - t, y + m + g, x + w, y + h - t - g),
    }
    lit = _SEG.get(ch, "")
    for k, r in rects.items():
        d.rounded_rectangle(r, radius=t // 2, fill=on if k in lit else off)


def _led(img, c, hm, date, now):
    img = _dim(img, 0.6)
    panel = _layer()
    ImageDraw.Draw(panel).rounded_rectangle((50, 230, S - 50, 570), radius=36, fill=(6, 6, 10, 215),
                                            outline=shade(c, 0.4) + (255,), width=4)
    img = _over(img, panel)
    dw, dh, t, gap = 112, 230, 20, 28
    total = 4 * dw + 3 * gap + 70
    x = (S - total) / 2
    on_l = _layer()
    d_on = ImageDraw.Draw(on_l)
    off_l = _layer()
    d_off = ImageDraw.Draw(off_l)
    chars = hm.replace(":", "")
    y = 285
    for i, ch in enumerate(chars):
        _seg_digit(d_off, "8", x, y, dw, dh, t, (255, 255, 255, 0), shade(c, 0.86) + (255,))
        _seg_digit(d_on, ch, x, y, dw, dh, t, c + (255,), (0, 0, 0, 0))
        x += dw + gap
        if i == 1:
            for yy in (y + dh * 0.33, y + dh * 0.67):
                d_on.ellipse((x + 6, yy - 11, x + 28, yy + 11), fill=c + (255,))
            x += 70
    img = _over(img, off_l)
    img = _glow_from_mask(img, on_l.split()[3], c, 14, 1.5, 2)
    img = _over(img, on_l)
    hot = _layer()
    hd = ImageDraw.Draw(hot)
    hd.rectangle((0, 0, 0, 0))
    img = plain_text(img, date, (S / 2, 640), _font("DejaVuSansMono-Bold.ttf", 40), tint(c, 0.5) + (255,), shadow=False)
    return img


def _synth(img, c, hm, date, now):
    img = _dim(img, 0.35, vignette=False)
    top, bot = _hue(c, -0.08, 0.95, 0.35), (10, 4, 28)
    bg = _vgrad(S, S, top, bot).convert("RGBA")
    bg.putalpha(Image.new("L", (S, S), 190))
    img = _over(img, bg)
    sun_c1, sun_c2 = (255, 214, 64), _hue(c, 0, 1.0, 1.0)
    ss = _SS()
    cx, cy, r = S / 2, 330, 210
    sun = _vgrad(S, S, sun_c1, sun_c2).convert("RGBA")
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    md = ImageDraw.Draw(mask)
    for i in range(8):                                        # stripes cut out of the lower half
        yy = cy + 20 + i * 24
        md.rectangle((0, yy, S, yy + 3 + i * 2), fill=0)
    sun.putalpha(mask)
    img = _glow_from_mask(img, mask, c, 30, 1.0, 1)
    img = _over(img, sun)
    lay = _layer()
    d = ImageDraw.Draw(lay)
    hz = 560
    d.rectangle((0, hz, S, S), fill=(8, 2, 22, 235))
    for k in range(1, 12):
        yy = hz + (k ** 2) * 2.1
        d.line([(0, yy), (S, yy)], fill=c + (220,), width=3)
    for k in range(-14, 15):
        d.line([(S / 2 + k * 14, hz), (S / 2 + k * 90, S)], fill=c + (200,), width=3)
    d.line([(0, hz), (S, hz)], fill=tint(c, 0.6) + (255,), width=4)
    img = _over(img, lay)
    f = _fit("Poppins-Bold.ttf", hm, 520, 190)
    img = glow_text(img, hm, (cx, cy + 6), f, c, radius=12, core=(255, 255, 255))
    return plain_text(img, date, (cx, 730), _font("Poppins-Medium.ttf", 40), (255, 255, 255, 240))


def _gold(img, c, hm, date, now):
    img = _dim(img, 0.6)
    g1, g2, g3 = tint(c, 0.8), c, shade(c, 0.45)
    lay = _layer()
    d = ImageDraw.Draw(lay)
    d.rounded_rectangle((36, 36, S - 36, S - 36), radius=40, outline=g2 + (255,), width=5)
    d.rounded_rectangle((56, 56, S - 56, S - 56), radius=30, outline=g2 + (140,), width=2)
    for (x, y) in ((36, 36), (S - 36, 36), (36, S - 36), (S - 36, S - 36)):
        d.ellipse((x - 11, y - 11, x + 11, y + 11), fill=g1 + (255,))
    img = _over(img, lay)
    f = _fit("Poppins-Bold.ttf", hm, S * 0.78, 260)
    m = _mask_text(hm, (S / 2, S / 2 - 10), f)
    img = _glow_from_mask(img, m, g2, 20, 0.8, 1)
    grad = Image.new("RGB", (S, S))
    gd = ImageDraw.Draw(grad)
    for y in range(S):
        t = abs(((y - 250) / 300))
        t = min(t, 1)
        col = tuple(int(g1[i] * (1 - t) + g3[i] * t) if y < S / 2 - 10 else int(g3[i] * (1 - t) * 0.0 + (g2[i] * (1 - t) + g3[i] * t)) for i in range(3))
        gd.line([(0, y), (S, y)], fill=col)
    gl = grad.convert("RGBA")
    gl.putalpha(m)
    img = _over(img, gl)
    ln = _layer()
    ImageDraw.Draw(ln).line([(S / 2 - 160, 600), (S / 2 + 160, 600)], fill=g2 + (255,), width=3)
    ImageDraw.Draw(ln).ellipse((S / 2 - 7, 593, S / 2 + 7, 607), fill=g1 + (255,))
    img = _over(img, ln)
    return plain_text(img, date, (S / 2, 660), _font("Poppins-Medium.ttf", 44), g1 + (255,))


def _matrix(img, c, hm, date, now):
    img = _dim(img, 0.72)
    rnd = random.Random(now.hour * 60 + now.minute)
    f = _font("DejaVuSansMono-Bold.ttf", 28)
    lay = _layer()
    d = ImageDraw.Draw(lay)
    chars = "01234567890ABCDEF<>=+*#"
    for x in range(8, S, 30):
        head = rnd.randint(2, 26)
        ln = rnd.randint(6, 18)
        for i in range(ln):
            yy = (head - i) * 32
            if 0 <= yy < S:
                a = int(255 * (1 - i / ln))
                col = tint(c, 0.75) if i == 0 else c
                d.text((x, yy), rnd.choice(chars), font=f, fill=col + (a,))
    img = _over(img, lay)
    box = _layer()
    ImageDraw.Draw(box).rounded_rectangle((70, 270, S - 70, 530), radius=26, fill=(0, 0, 0, 205), outline=c + (255,), width=3)
    img = _over(img, box)
    fb = _fit("DejaVuSansMono-Bold.ttf", hm, S - 220, 230)
    img = glow_text(img, hm, (S / 2, 385), fb, c, radius=12, core=tint(c, 0.8))
    return plain_text(img, f"[ {date} ]", (S / 2, 492), _font("DejaVuSansMono-Bold.ttf", 34), tint(c, 0.6) + (255,), shadow=False)


def _orbit(img, c, hm, date, now):
    img = _dim(img, 0.55)
    cx = cy = S / 2
    ss = _SS()
    rings = ((310, now.minute / 60 * 360, 16), (228, ((now.hour % 12) + now.minute / 60) / 12 * 360, 20))
    for R, ang, pr in rings:
        ss.d.ellipse(ss.xy(cx - R, cy - R, cx + R, cy + R), outline=(255, 255, 255, 70), width=4)
        ss.d.arc(ss.xy(cx - R, cy - R, cx + R, cy + R), -90, -90 + ang, fill=c + (255,), width=10)
    ss.d.ellipse(ss.xy(cx - 150, cy - 150, cx + 150, cy + 150), outline=(255, 255, 255, 40), width=3)
    for R, ang, pr in rings:
        px, py = _polar(cx, cy, R, ang)
        ss.d.ellipse(ss.xy(px - pr, py - pr, px + pr, py + pr), fill=tint(c, 0.7) + (255,))
        ss.d.ellipse(ss.xy(px - pr / 2.4, py - pr / 2.4, px + pr / 2.4, py + pr / 2.4), fill=c + (255,))
    ring = ss.done()
    img = _glow_from_mask(img, ring.split()[3], c, 14, 1.2, 1)
    img = _over(img, ring)
    f = _fit("Poppins-Bold.ttf", hm, 330, 130)
    img = plain_text(img, hm, (cx, cy - 6), f, (255, 255, 255, 255))
    return plain_text(img, date, (cx, cy + 74), _font("Poppins-Medium.ttf", 30), tint(c, 0.6) + (255,))


def _ecg(img, c, hm, date, now):
    img = _dim(img, 0.6)
    grid = _layer()
    gd = ImageDraw.Draw(grid)
    for k in range(0, S, 40):
        gd.line([(k, 0), (k, S)], fill=c + (26,))
        gd.line([(0, k), (S, k)], fill=c + (26,))
    img = _over(img, grid)
    f = _fit("Poppins-Bold.ttf", hm, S * 0.82, 260)
    img = glow_text(img, hm, (S / 2, 285), f, c, radius=18, core=(255, 255, 255))
    y0 = 520
    rnd = random.Random(now.minute)
    pts, x = [], 30
    while x < S - 30:
        pts += [(x, y0), (x + 14, y0)]
        x += 14
        if rnd.random() < 0.28 or x > S / 2 - 40 and x < S / 2 + 40:
            pts += [(x + 8, y0 - 18), (x + 18, y0 + 14), (x + 30, y0 - 120 - rnd.randint(0, 30)), (x + 42, y0 + 70), (x + 52, y0 - 10), (x + 62, y0)]
            x += 70
        else:
            x += 20
    m = Image.new("L", (S, S), 0)
    md = ImageDraw.Draw(m)
    md.line(pts, fill=255, width=7, joint="curve")
    img = _glow_from_mask(img, m, c, 9, 1.7, 2)
    lay = _layer()
    ImageDraw.Draw(lay).line(pts, fill=tint(c, 0.8) + (255,), width=4, joint="curve")
    img = _over(img, lay)
    return plain_text(img, date, (S / 2, 690), _font("Poppins-Medium.ttf", 42), (255, 255, 255, 235))


def _flip(img, c, hm, date, now):
    img = _dim(img, 0.55)
    hh, mm = hm.split(":")
    f = _font("Poppins-Bold.ttf", 300)
    for i, (txt, x0) in enumerate(((hh, 52), (mm, S / 2 + 18))):
        box = (x0, 190, x0 + 330, 560)
        sh = Image.new("RGBA", (S, S), (0, 0, 0, 255))
        sh.putalpha(_round_mask((box[0] + 4, box[1] + 14, box[2] + 4, box[3] + 14), 34).filter(ImageFilter.GaussianBlur(16)).point(lambda p: int(p * 0.7)))
        img = _over(img, sh)
        card = _vgrad(S, S, (52, 52, 60), (20, 20, 26)).convert("RGBA")
        card.putalpha(_round_mask(box, 34))
        img = _over(img, card)
        lay = _layer()
        d = ImageDraw.Draw(lay)
        d.rounded_rectangle(box, radius=34, outline=(255, 255, 255, 40), width=2)
        img = _over(img, lay)
        img = plain_text(img, txt, ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2 + 6), f, (255, 255, 255, 255), shadow=False)
        lay = _layer()
        d = ImageDraw.Draw(lay)
        mid = (box[1] + box[3]) / 2
        d.rectangle((box[0], mid - 3, box[2], mid + 3), fill=(0, 0, 0, 230))
        d.ellipse((box[0] - 7, mid - 12, box[0] + 13, mid + 12), fill=(10, 10, 14, 255))
        d.ellipse((box[2] - 13, mid - 12, box[2] + 7, mid + 12), fill=(10, 10, 14, 255))
        d.rounded_rectangle((box[0] + 30, box[3] + 14, box[2] - 30, box[3] + 24), radius=5, fill=c + (255,))
        img = _over(img, lay)
    for yy in (330, 420):
        lay = _layer()
        ImageDraw.Draw(lay).ellipse((S / 2 - 8, yy - 8, S / 2 + 8, yy + 8), fill=c + (255,))
        img = _over(img, lay)
    return plain_text(img, date, (S / 2, 680), _font("Poppins-Medium.ttf", 44), (255, 255, 255, 235))


STYLES += [("aurora", "شفق"), ("glitch", "گلیچ"), ("led", "ال‌ای‌دی"), ("synth", "سینت‌ویو"),
           ("gold", "طلایی لوکس"), ("matrix", "ماتریکس"), ("orbit", "مدار"), ("ecg", "ضربان"), ("flip", "فلیپ")]


_DRAW = {"neon": _neon, "stack": _stack, "ring": _ring, "analog": _analog, "glass": _glass,
         "hud": _hud, "capsule": _capsule, "minimal": _minimal,
         "aurora": _aurora, "glitch": _glitch, "led": _led, "synth": _synth, "gold": _gold,
         "matrix": _matrix, "orbit": _orbit, "ecg": _ecg, "flip": _flip}


def style_key(v):
    keys = [k for k, _ in STYLES]
    if isinstance(v, int) or str(v).lstrip("-").isdigit():
        return keys[int(v) % len(keys)]
    return v if v in _DRAW else keys[0]


def color_key(v):
    return v if v in COLORS else COLOR_ORDER[0]


def _date_text(now):
    try:
        import jdatetime
        return jdatetime.datetime.fromgregorian(datetime=now).strftime("%Y/%m/%d")
    except Exception:                                       # noqa: BLE001
        return now.strftime("%Y-%m-%d")


def render(raw, style, color, now=None) -> bytes:
    now = now or datetime.now()
    ck = color_key(color)
    col = COLORS[ck][2]
    img = make_base(raw, col)
    img = _DRAW[style_key(style)](img, col, now.strftime("%H:%M"), _date_text(now), now)
    bio = io.BytesIO()
    img.convert("RGB").save(bio, "JPEG", quality=92, optimize=True)
    return bio.getvalue()
