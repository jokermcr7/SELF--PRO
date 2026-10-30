"""Clock-on-profile-photo renderer (Pillow).

render(raw_base_photo_bytes | None, style, color, now) -> JPEG bytes (800x800)

29 designs x 7 colors. The base photo is the user's own profile picture (or a gradient if there is none);
every design is drawn ON TOP of it, so the photo stays visible.
"""
import functools
import io
import math
import os
from datetime import datetime

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

S = 800
OUT = 1280           # uploaded size (profile photos are stored/zoomed up to this)
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
    "orange": ("نارنجی", "🟠", (255, 132, 24)),
    "white": ("سفید", "⚪", (212, 220, 240)),
    "dark": ("دارک", "⚫", (150, 158, 184)),      # black glass look: extra-dark photo + cool silver accent
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


def normalize_base(raw, size=1024):
    """Square-crop + shrink an uploaded photo so it is cheap to store (JPEG bytes)."""
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    im = ImageOps.fit(im, (size, size), Image.LANCZOS)
    bio = io.BytesIO()
    im.save(bio, "JPEG", quality=95, subsampling=0)
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


# ------------------------------------------------------------------ lion & sun (شیر و خورشید) + 8 more designs
def _cr(pts, n=14, closed=False):
    P = list(pts)
    P = ([P[-1]] + P + [P[0], P[1]]) if closed else ([P[0]] + P + [P[-1]])
    out = []
    for i in range(1, len(P) - 2):
        p0, p1, p2, p3 = P[i-1], P[i], P[i+1], P[i+2]
        for j in range(n):
            t = j / n; t2, t3 = t*t, t*t*t
            out.append(tuple(0.5*((2*p1[c]) + (-p0[c]+p2[c])*t + (2*p0[c]-5*p1[c]+4*p2[c]-p3[c])*t2 + (-p0[c]+3*p1[c]-3*p2[c]+p3[c])*t3) for c in (0, 1)))
    if not closed:
        out.append(P[-2])
    return out

def _radial(r, inner, outer, steps=16):
    """Concentric-circle gradient disc (RGBA, size 2r x 2r)."""
    im = Image.new("RGBA", (2 * r, 2 * r), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for i in range(steps):
        t = i / (steps - 1)
        rr = r * (1 - t)
        col = tuple(int(outer[j] * (1 - t) + inner[j] * t) for j in range(3))
        d.ellipse((r - rr, r - rr, r + rr, r + rr), fill=col + (255,))
    return im


def _metal(top, mid, bot, y0, y1):
    """Vertical metallic gradient image (RGB, S x S)."""
    g = Image.new("RGB", (S, S))
    d = ImageDraw.Draw(g)
    for y in range(S):
        t = min(max((y - y0) / max(y1 - y0, 1), 0), 1)
        if t < 0.5:
            u = t / 0.5
            col = tuple(int(top[i] * (1 - u) + mid[i] * u) for i in range(3))
        else:
            u = (t - 0.5) / 0.5
            col = tuple(int(mid[i] * (1 - u) + bot[i] * u) for i in range(3))
        d.line([(0, y), (S, y)], fill=col)
    return g


def _god_rays(img, cx, cy, c, count=18, length=900, alpha=46, seed=0):
    """Soft light beams fanning out from (cx, cy)."""
    lay = _layer()
    d = ImageDraw.Draw(lay)
    rnd = random.Random(seed)
    for i in range(count):
        a = i * 360 / count + rnd.uniform(-4, 4)
        w = rnd.uniform(1.6, 4.2)
        d.polygon([(cx, cy), _polar(cx, cy, length, a - w), _polar(cx, cy, length, a + w)], fill=tint(c, 0.35) + (alpha,))
    return _over(img, lay.filter(ImageFilter.GaussianBlur(6)))


def _embers(img, c, now, n=46, area=(0, 0, S, S), seed_extra=0):
    rnd = random.Random(now.hour * 60 + now.minute + seed_extra)
    lay = _layer()
    d = ImageDraw.Draw(lay)
    for _ in range(n):
        x, y = rnd.randint(area[0], area[2]), rnd.randint(area[1], area[3])
        r = rnd.choice((1.6, 2.2, 2.8, 3.6))
        d.ellipse((x - r, y - r, x + r, y + r), fill=tint(c, 0.55) + (rnd.randint(120, 255),))
    g = lay.filter(ImageFilter.GaussianBlur(4))
    return _over(_over(img, g), lay)



# ------------------------------------------------------------------ Lion & Sun emblem (crowned, laurel wreath) as crystal
class _EC:
    """100x100 design grid -> supersampled L masks."""
    def __init__(self, size=800, ss=2, w=100, h=100):
        self.N = size * ss; self.size = size; self.ss = ss
        self.k = self.N / 100.0
        self.m = Image.new("L", (self.N, self.N), 0)
        self.d = ImageDraw.Draw(self.m)

    def P(self, x, y): return (x * self.k, y * self.k)

    def disc(self, x, y, r, v=255, d=None):
        (d or self.d).ellipse((self.P(x - r, y - r) + self.P(x + r, y + r)), fill=v)

    def ell(self, x, y, rx, ry, rot=0, v=255, d=None):
        pts = []
        for i in range(48):
            a = 2 * math.pi * i / 48
            px, py = rx * math.cos(a), ry * math.sin(a)
            c, s = math.cos(math.radians(rot)), math.sin(math.radians(rot))
            pts.append(self.P(x + px * c - py * s, y + px * s + py * c))
        (d or self.d).polygon(pts, fill=v)

    def tube(self, pts, w0, w1, n=12, v=255, d=None):
        path = _cr(pts, n=n)
        for i, (x, y) in enumerate(path):
            self.disc(x, y, (w0 + (w1 - w0) * i / max(len(path) - 1, 1)) / 2, v, d)

    def poly(self, pts, n=10, v=255, d=None, closed=True):
        (d or self.d).polygon([self.P(*p) for p in _cr(pts, n=n, closed=closed)], fill=v)

    def flat(self, pts, v=255, d=None):
        (d or self.d).polygon([self.P(*p) for p in pts], fill=v)

    def line(self, pts, w=0.6, v=255, n=10, d=None, smooth=True):
        path = _cr(pts, n=n) if smooth else pts
        (d or self.d).line([self.P(*p) for p in path], fill=v, width=max(1, int(w * self.k)), joint="curve")

    def leaf(self, x, y, ang, length, width, v=255, d=None):
        L, W = [], []
        for i in range(11):
            t = i / 10
            hw = width / 2 * math.sin(math.pi * t) ** 0.75
            L.append((t * length, -hw)); W.append((t * length, hw))
        a = math.radians(ang); c, s = math.cos(a), math.sin(a)
        pts = [(x + px * c - py * s, y + px * s + py * c) for px, py in L + W[::-1]]
        self.flat(pts, v, d)

    def out(self):
        return self.m.resize((self.size, self.size), Image.LANCZOS)


def _emblem_build(size=800, ss=2):
    """Heraldic Lion & Sun with crown, laurel wreath and bow. Returns dict of L masks:
       fill (all solid shapes), cut (engraved lines), lion, sun, wreath, crown"""
    layers = {}

    def new(): return _EC(size, ss)

    # ------------------------------------------------------------ laurel wreath + bow
    W = new()
    cx0, cy0, R = 50, 54.5, 33
    for sgn in (1, -1):
        def X(x): return x if sgn == 1 else 100 - x
        pts = []
        for th in range(96, 246, 6):                  # bottom -> top, left side (th in deg, y down)
            a = math.radians(th)
            pts.append((cx0 + R * math.cos(a), cy0 + R * math.sin(a)))
        pts = [(X(x), y) for x, y in pts]
        W.tube(pts, 1.1, 0.8, n=6)
        for i, th in enumerate(range(100, 242, 9)):
            a = math.radians(th)
            px, py = cx0 + R * math.cos(a), cy0 + R * math.sin(a)
            tang = th + 90                            # growth direction (towards increasing th)
            for side, ln, wd in ((-1, 8.6, 3.5), (1, 7.2, 3.0)):
                ang = tang + side * 42
                ax, ay = px, py
                if sgn == -1:
                    ax = 100 - px
                    ang = 180 - ang
                W.leaf(ax, ay, ang, ln, wd)
        a = math.radians(246)
        tx, ty = cx0 + R * math.cos(a), cy0 + R * math.sin(a)
        W.leaf(X(tx), ty, (336 if sgn == 1 else 180 - 336), 8, 3.2)
    # bow
    for sgn in (1, -1):
        X = (lambda x: x) if sgn == 1 else (lambda x: 100 - x)
        W.ell(X(43.5), 89.7, 6.4, 3.0, rot=(-22 if sgn == 1 else 22))
        W.flat([(X(47), 91), (X(41.5), 96.5), (X(45), 97.2), (X(49.5), 93)])
    W.disc(50, 90.1, 2.4)
    layers["wreath"] = W.out()

    # ------------------------------------------------------------ crown
    C = new()
    C.flat([(37.5, 21.5), (35.5, 11.5), (41.5, 15.5), (44, 8.5), (50, 14.5), (56, 8.5), (58.5, 15.5), (64.5, 11.5), (62.5, 21.5)])
    C.ell(50, 12.5, 7.8, 5.4)
    C.ell(50, 5.2, 2.6, 3.8)                         # finial tuft
    C.disc(50, 1.6, 1.4)
    for x, y in ((35.5, 10.4), (44, 7.4), (56, 7.4), (64.5, 10.4)):
        C.disc(x, y, 1.7)
    C.m = C.m
    C.d.rounded_rectangle((C.P(36.4, 20.6) + C.P(63.6, 26.2)), radius=int(1.4 * C.k), fill=255)
    layers["crown"] = C.out()
    cc = new()
    cc.line([(41, 17), (50, 19.5), (59, 17)], 0.45, 255)
    for x in (42.5, 50, 57.5):
        cc.disc(x, 23.4, 1.0)
    cc.line([(46.5, 8.5), (47.2, 14)], 0.4); cc.line([(53.5, 8.5), (52.8, 14)], 0.4)
    layers["crown_cut"] = cc.out()

    # ------------------------------------------------------------ sun (behind the lion)
    S_ = new()
    sx, sy, sr = 64.5, 44, 6.4
    for i in range(24):
        a = math.radians(i * 15)
        if i % 2 == 0:
            p = [(sx + sr * 1.05 * math.cos(a - 0.12), sy + sr * 1.05 * math.sin(a - 0.12)), (sx + sr * 2.1 * math.cos(a), sy + sr * 2.1 * math.sin(a)),
                 (sx + sr * 1.05 * math.cos(a + 0.12), sy + sr * 1.05 * math.sin(a + 0.12))]
            S_.flat(p)
        else:
            L, Rr = [], []
            for j in range(11):
                t = j / 10
                rad = sr * 1.05 + t * sr * 0.95
                ang = a + 0.16 * math.sin(t * math.pi * 2.2)
                w = 0.15 * (1 - t) ** 0.7 + 0.01
                L.append((sx + rad * math.cos(ang - w), sy + rad * math.sin(ang - w)))
                Rr.append((sx + rad * math.cos(ang + w), sy + rad * math.sin(ang + w)))
            S_.flat(L + Rr[::-1])
    S_.disc(sx, sy, sr)
    layers["sun"] = S_.out()
    sc = new()
    sc.d.ellipse((sc.P(sx - sr * 1.16, sy - sr * 1.16) + sc.P(sx + sr * 1.16, sy + sr * 1.16)), outline=255, width=max(1, int(0.5 * sc.k)))
    # tiny serene face
    for e in (-1, 1):
        sc.line([(sx + e * 2.7 - 1.3, sy - 1.3), (sx + e * 2.7, sy - 1.9), (sx + e * 2.7 + 1.3, sy - 1.3)], 0.4)
    sc.line([(sx, sy - 1.0), (sx - 0.4, sy + 1.2), (sx + 0.6, sy + 1.4)], 0.35)
    sc.line([(sx - 2.0, sy + 2.9), (sx, sy + 3.6), (sx + 2.0, sy + 2.9)], 0.45)
    layers["sun_cut"] = sc.out()

    # ------------------------------------------------------------ lion (frontal head, profile body facing left)
    Ln = new()
    # hind legs + paws
    Ln.tube([(73.5, 59), (71, 67.5), (75, 73), (73.5, 77.5)], 8.2, 2.8)
    Ln.ell(73, 78, 4.6, 1.3)
    Ln.tube([(67.5, 59), (65, 68), (68.5, 73.5), (66.5, 77.5)], 8.8, 3.0)
    Ln.ell(66, 78, 4.8, 1.3)
    # standing front leg
    Ln.tube([(54, 60), (54.6, 68.5), (53, 73.5), (52, 77.5)], 6.2, 2.7)
    Ln.ell(51.4, 78, 4.3, 1.3)
    # torso
    Ln.poly([(49.5, 52.5), (57, 51), (65.5, 51.8), (72.5, 52), (77, 56), (76, 62), (71, 65.5), (64, 62.5), (57, 63), (51.5, 65), (47.5, 61)], n=8)
    # tail (S curve) + tuft
    Ln.tube([(76.5, 55), (80.5, 52), (81.2, 46.5), (79, 42.5), (75.8, 41.8)], 2.0, 1.4, n=8)
    Ln.flat([(75.8, 42.8), (73.2, 39), (76, 39.8), (76.8, 36.2), (78.8, 40), (81, 38.6), (79.4, 43.6)])
    # raised paw with sword, clear of the mane
    Ln.tube([(49.5, 60.5), (43.5, 65), (36, 63), (31.5, 57.5)], 5.8, 4.0)
    Ln.disc(30.6, 56, 3.0)
    # mane: ring of curls around the frontal face + trailing locks over the shoulders
    hx, hy = 47.5, 49.5
    for ring, (rad, br, cnt, off) in enumerate(((9.0, 3.0, 19, 0), (11.4, 2.2, 22, 8))):
        for i in range(cnt):
            a = math.radians(off + i * 360 / cnt)
            Ln.disc(hx + rad * math.cos(a), hy + rad * 1.02 * math.sin(a), br + 0.3 * math.sin(i * 1.9))
    Ln.disc(hx, hy, 9.8)
    for (x, y, r) in ((55.5, 46.5, 2.8), (59.5, 48, 2.5), (63, 50.5, 2.2), (55, 57.5, 3.2), (59, 58.5, 2.5)):
        Ln.disc(x, y, r)
    # ears, face
    Ln.disc(41, 43.5, 2.2); Ln.disc(54, 43.5, 2.2)
    Ln.ell(hx, hy + 1, 6.4, 7.2)
    # scimitar blade (crescent), hilt in paw
    blade = _cr([(30.4, 55), (28.2, 47), (28.4, 40), (31.5, 35), (36.5, 33)], n=10)
    Lf, Rt = [], []
    for i, (x, y) in enumerate(blade):
        w = 1.05 * (1 - (i / (len(blade) - 1)) ** 1.8) + 0.1
        Lf.append((x - w, y)); Rt.append((x + w, y))
    Ln.flat(Lf + Rt[::-1])
    Ln.line([(27.6, 56), (33.6, 57)], 1.1, smooth=False)
    layers["lion"] = Ln.out()

    Lc = new()
    # face details (cut lines)
    for e in (-1, 1):
        ex = hx + e * 2.8
        Lc.line([(ex - 1.5 * e, hy - 0.5), (ex, hy - 1.3), (ex + 1.6 * e, hy - 0.4), (ex, hy + 0.3), (ex - 1.5 * e, hy - 0.5)], 0.35)
        Lc.line([(ex - 2.0, hy - 2.7), (ex + 0.2 * e, hy - 3.1), (ex + 2.0, hy - 2.4)], 0.4)
    Lc.line([(hx, hy - 1.5), (hx, hy + 2.6)], 0.45)                      # nose bridge
    Lc.flat([(hx - 1.9, hy + 2.5), (hx + 1.9, hy + 2.5), (hx, hy + 4.0)])  # nose
    Lc.line([(hx, hy + 4), (hx, hy + 5.3)], 0.4)
    Lc.line([(hx - 2.8, hy + 5.6), (hx - 1.1, hy + 5.1), (hx, hy + 5.0), (hx + 1.1, hy + 5.1), (hx + 2.8, hy + 5.6)], 0.38)
    Lc.line([(hx - 2.0, hy + 6.6), (hx, hy + 7.2), (hx + 2.0, hy + 6.6)], 0.38)
    for e in (-1, 1):                                                    # whisker dots
        for (dx, dy) in ((2.2, 4.2), (3.2, 5.0), (2.0, 5.4)):
            Lc.disc(hx + e * dx, hy + dy, 0.28)
    # mane curl lines
    for a0 in range(0, 360, 36):
        a = math.radians(a0 + 6)
        Lc.line([(hx + 7.6 * math.cos(a), hy + 7.8 * math.sin(a)), (hx + 9.6 * math.cos(a + 0.12), hy + 9.8 * math.sin(a + 0.12))], 0.32)
    # body / leg details
    Lc.line([(58, 55), (60, 60), (58, 63)], 0.35); Lc.line([(67.5, 54), (70.5, 59), (68.5, 64)], 0.4)
    Lc.line([(54, 63), (54.6, 67)], 0.3)
    layers["lion_cut"] = Lc.out()
    return layers




# ------------------------------------------------------------------ the REAL Lion & Sun emblem (from the reference flag image)
@functools.lru_cache(maxsize=1)
def _emblem_src():
    return Image.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "lion_sun.png")).convert("RGBA")


@functools.lru_cache(maxsize=8)
def _emblem_img(size):
    """The flag emblem (gold lion, sun, crown, laurel wreath, bow) fitted into a size x size box."""
    im = _emblem_src()
    k = size / max(im.size)
    out = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.LANCZOS)
    return out.filter(ImageFilter.UnsharpMask(radius=1.1, percent=60, threshold=2))


def _emblem_draw(img, size, pos, c, glow=0.8, glow_r=26):
    """Draw the emblem exactly as in the reference (original gold), with a soft shadow, an accent-coloured glow and a sheen."""
    em = _emblem_img(size)
    full = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    full.paste(em, (pos[0] + (size - em.width) // 2, pos[1] + (size - em.height) // 2))
    a = full.split()[3]
    sh = Image.new("RGBA", (S, S), (0, 0, 0, 255))
    sh.putalpha(a.filter(ImageFilter.GaussianBlur(12)).point(lambda p: int(p * 0.6)))
    img = _over(img, ImageChops.offset(sh, 4, 10))
    if glow:
        img = _glow_from_mask(img, a, c, glow_r, glow, 1)
    img = _over(img, full)
    bb = a.getbbox()
    if bb:                                   # light sweep + edge highlight (keeps the original gold look)
        g = Image.new("L", (S, S))
        gd = ImageDraw.Draw(g)
        for y in range(bb[1], bb[3]):
            t = (y - bb[1]) / max(bb[3] - bb[1], 1)
            gd.line([(0, y), (S, y)], fill=int(255 * 0.22 * (1 - t) ** 1.5))
        sheen = Image.new("RGBA", (S, S), (255, 244, 205, 255))
        sheen.putalpha(ImageChops.multiply(g, a))
        img = _over(img, sheen)
        lit = ImageChops.subtract(a, ImageChops.offset(a, 2, 2)).filter(ImageFilter.GaussianBlur(0.6))
        hl = Image.new("RGBA", (S, S), (255, 250, 225, 255))
        hl.putalpha(lit.point(lambda p: int(p * 0.7)))
        img = _over(img, hl)
    return img


@functools.lru_cache(maxsize=6)
def _emblem(size):
    return _emblem_build(size)


def _full(m, pos):
    f = Image.new("L", (S, S), 0)
    f.paste(m, pos)
    return f


def _emblem_shape(size, pos):
    """Solid emblem as one L mask: the sun is cut away where the lion overlaps it, details are engraved gaps."""
    ly = _emblem(size)
    k = max(1, int(round(size / 800 * 4))) * 2 + 1
    lion, sun, wreath, crown = (_full(ly[n], pos) for n in ("lion", "sun", "wreath", "crown"))
    sun2 = ImageChops.subtract(sun, lion.filter(ImageFilter.MaxFilter(k)))
    solid = ImageChops.lighter(ImageChops.lighter(sun2, lion), ImageChops.lighter(wreath, crown))
    cut = ImageChops.lighter(ImageChops.lighter(_full(ly["lion_cut"], pos), _full(ly["sun_cut"], pos)), _full(ly["crown_cut"], pos))
    return ImageChops.subtract(solid, ImageChops.multiply(cut, solid))


def _crystal(img, shape, c, depth=3, frost=0.2, tintk=0.26, glow=0.9, glow_r=22, shadow=True):
    """Draw an L mask as frosted crystal: blurred backdrop, colour tint, sheen, light streaks and a bevelled edge."""
    bb = shape.getbbox()
    if not bb:
        return img
    if shadow:
        sh = Image.new("RGBA", (S, S), (0, 0, 0, 255))
        sh.putalpha(shape.filter(ImageFilter.GaussianBlur(14)).point(lambda p: int(p * 0.55)))
        img = _over(img, ImageChops.offset(sh, 5, 12))
    if glow:
        img = _glow_from_mask(img, shape, c, glow_r, glow, 1)
    body = _over(img.filter(ImageFilter.GaussianBlur(12)), Image.new("RGBA", (S, S), (255, 255, 255, int(255 * frost))))
    body = _over(body, Image.new("RGBA", (S, S), c + (int(255 * tintk),)))
    g = Image.new("L", (S, S))
    gd = ImageDraw.Draw(g)
    for y in range(S):
        t = min(max((y - bb[1]) / max(bb[3] - bb[1], 1), 0), 1)
        gd.line([(0, y), (S, y)], fill=int(255 * (0.34 * (1 - t) ** 1.4 + 0.04)))
    wl = Image.new("RGBA", (S, S), (255, 255, 255, 255))
    wl.putalpha(g)
    body = _over(body, wl)
    st = _layer()
    d = ImageDraw.Draw(st)
    for off, w, a in ((-120, 70, 52), (120, 26, 40), (260, 16, 30)):
        d.polygon([(off, S), (off + w, S), (off + w + S * 0.9, 0), (off + S * 0.9, 0)], fill=(255, 255, 255, a))
    body = _over(body, st)
    body.putalpha(shape)
    img = _over(img, body)
    lit = ImageChops.subtract(shape, ImageChops.offset(shape, depth, depth))
    dark = ImageChops.subtract(shape, ImageChops.offset(shape, -depth, -depth))
    inner = ImageChops.subtract(shape, shape.filter(ImageFilter.MinFilter(2 * depth + 1))).filter(ImageFilter.GaussianBlur(3))
    for col, m, k in ((tint(c, 0.35), inner, 0.55), (shade(c, 0.7), dark.filter(ImageFilter.GaussianBlur(0.8)), 0.78),
                      ((255, 255, 255), lit.filter(ImageFilter.GaussianBlur(0.7)), 1.0),
                      (tint(c, 0.8), ImageChops.subtract(shape.filter(ImageFilter.MaxFilter(3)), shape), 0.6)):
        lay = Image.new("RGBA", (S, S), col + (255,))
        lay.putalpha(m.point(lambda p, k=k: min(255, int(p * k))))
        img = _over(img, lay)
    return img


def _star(img, x, y, r):
    lay = _layer()
    ImageDraw.Draw(lay).polygon([(x, y - r), (x + r * 0.13, y - r * 0.13), (x + r, y), (x + r * 0.13, y + r * 0.13), (x, y + r),
                                 (x - r * 0.13, y + r * 0.13), (x - r, y), (x - r * 0.13, y - r * 0.13)], fill=(255, 255, 255, 255))
    return _over(_over(img, lay.filter(ImageFilter.GaussianBlur(6))), lay)


def _crystal_panel(img, box, radius, c):
    """Frosted glass plate (backdrop blur, soft shadow, bevelled border)."""
    m = _round_mask(box, radius)
    sh = Image.new("RGBA", (S, S), (0, 0, 0, 255))
    sh.putalpha(m.filter(ImageFilter.GaussianBlur(20)).point(lambda p: int(p * 0.6)))
    img = _over(img, ImageChops.offset(sh, 0, 14))
    body = _over(img.filter(ImageFilter.GaussianBlur(16)), Image.new("RGBA", (S, S), (255, 255, 255, 34)))
    body = _over(body, Image.new("RGBA", (S, S), c + (30,)))
    body.putalpha(m)
    img = _over(img, body)
    lit = ImageChops.subtract(m, ImageChops.offset(m, 3, 3))
    dark = ImageChops.subtract(m, ImageChops.offset(m, -3, -3))
    for col, mm, k in (((255, 255, 255), lit, 0.85), (shade(c, 0.6), dark, 0.55)):
        lay = Image.new("RGBA", (S, S), col + (255,))
        lay.putalpha(mm.filter(ImageFilter.GaussianBlur(0.8)).point(lambda p, k=k: int(p * k)))
        img = _over(img, lay)
    return img


def _crystal_text(img, text, pos, font, c, **kw):
    return _crystal(img, _mask_text(text, pos, font), c, **kw)


def _lionsun(img, c, hm, date, now):
    """شیر و خورشید بلوری ۱: crystal emblem on top, frosted glass clock plate with a minute bar below."""
    img = _dim(img, 0.5)
    img = _god_rays(img, S / 2, 250, c, count=14, alpha=26, seed=now.minute // 5)
    pos, size = ((S - 620) // 2, -52), 620
    img = _emblem_draw(img, size, pos, c)
    img = _star(img, S / 2, 30, 26)
    img = _crystal_panel(img, (150, 536, S - 150, 696), 44, c)
    f = _fit("Poppins-Bold.ttf", hm, 400, 104)
    img = plain_text(img, hm, (S / 2, 586), f, (255, 255, 255, 255))
    img = plain_text(img, date, (S / 2, 650), _font("Poppins-Medium.ttf", 26), (255, 255, 255, 225), shadow=False)
    bar = _layer()
    d = ImageDraw.Draw(bar)
    x0, x1, y = 200, S - 200, 676
    d.rounded_rectangle((x0, y - 3, x1, y + 3), radius=3, fill=(255, 255, 255, 70))
    xe = x0 + (x1 - x0) * (now.minute * 60 + now.second) / 3600
    d.rounded_rectangle((x0, y - 3, xe, y + 3), radius=3, fill=c + (255,))
    d.ellipse((xe - 7, y - 7, xe + 7, y + 7), fill=tint(c, 0.8) + (255,))
    return _over(_glow_from_mask(img, bar.split()[3], c, 6, 0.9, 1), bar)


def _lionsun2(img, c, hm, date, now):
    """شیر و خورشید بلوری ۲: crystal emblem on the left, big stacked glass digits next to it."""
    img = _dim(img, 0.5)
    pos, size = (-20, 112), 520
    img = _emblem_draw(img, size, pos, c)
    img = _star(img, pos[0] + size * 0.5, pos[1] + 12, 22)
    hh, mm = hm.split(":")
    f = _font("Poppins-Bold.ttf", 214)
    img = _crystal_text(img, hh, (594, 268), f, c, depth=4, frost=0.1, tintk=0.3, glow=1.1)
    img = _crystal_text(img, mm, (594, 490), f, c, depth=4, frost=0.1, tintk=0.3, glow=1.1)
    lay = _layer()
    d = ImageDraw.Draw(lay)
    d.line([(486, 382), (702, 382)], fill=c + (255,), width=4)
    d.ellipse((586, 374, 602, 390), fill=tint(c, 0.8) + (255,))
    d.line([(424, 210), (424, 590)], fill=tint(c, 0.4) + (120,), width=2)
    img = _over(_glow_from_mask(img, lay.split()[3], c, 8, 1.0, 1), lay)
    return plain_text(img, date, (594, 640), _font("Poppins-Medium.ttf", 34), (255, 255, 255, 235))


def _lionsun3(img, c, hm, date, now):
    """شیر و خورشید بلوری ۳: crystal emblem inside a watch ring whose arc fills with the minutes."""
    img = _dim(img, 0.55)
    cx = cy = S / 2
    R = 336
    ring = Image.new("L", (S * 2, S * 2), 0)
    rd = ImageDraw.Draw(ring)
    rd.ellipse(((cx - R - 9) * 2, (cy - R - 9) * 2, (cx + R + 9) * 2, (cy + R + 9) * 2), fill=255)
    rd.ellipse(((cx - R + 9) * 2, (cy - R + 9) * 2, (cx + R - 9) * 2, (cy + R - 9) * 2), fill=0)
    img = _crystal(img, ring.resize((S, S), Image.LANCZOS), c, depth=3, frost=0.1, tintk=0.18, glow=0.5)
    ss = _SS()
    d = ss.d
    X = lambda p: (p[0] * 2, p[1] * 2)
    for i in range(60):
        a = i * 6
        big = i % 5 == 0
        d.line([X(_polar(cx, cy, R - 26, a)), X(_polar(cx, cy, R - (46 if big else 38), a))], fill=(255, 255, 255, 255 if big else 150), width=6 if big else 3)
    img = _over(img, ss.done())
    frac = (now.minute * 60 + now.second) / 3600
    if frac > 0.003:
        arc = Image.new("L", (S * 2, S * 2), 0)
        ImageDraw.Draw(arc).arc((int((cx - R) * 2), int((cy - R) * 2), int((cx + R) * 2), int((cy + R) * 2)), -90, -90 + 360 * frac, fill=255, width=18)
        arc = arc.resize((S, S), Image.LANCZOS)
        img = _glow_from_mask(img, arc, c, 12, 1.4, 2)
        al = Image.new("RGBA", (S, S), tint(c, 0.55) + (255,))
        al.putalpha(arc)
        img = _over(img, al)
        hx, hy = _polar(cx, cy, R, 360 * frac)
        hd = _layer()
        ImageDraw.Draw(hd).ellipse((hx - 13, hy - 13, hx + 13, hy + 13), fill=(255, 255, 255, 255))
        img = _over(_glow_from_mask(img, hd.split()[3], c, 10, 1.6, 2), hd)
    pos, size = (170, 70), 460
    img = _emblem_draw(img, size, pos, c)
    img = _star(img, S / 2, pos[1] + 10, 20)
    f = _fit("Poppins-Bold.ttf", hm, 300, 84)
    img = _crystal_text(img, hm, (cx, 584), f, c, depth=3, frost=0.1, tintk=0.28, glow=1.0)
    return plain_text(img, date, (cx, 650), _font("Poppins-Medium.ttf", 28), (255, 255, 255, 230), shadow=False)


def _lionsun4(img, c, hm, date, now):
    """شیر و خورشید بلوری ۴ (الماسی): large golden-crystal emblem, light beams and thick glass numerals."""
    img = _dim(img, 0.62)
    halo = _layer()
    ImageDraw.Draw(halo).ellipse((S / 2 - 330, 20, S / 2 + 330, 560), fill=c + (120,))
    img = _over(img, halo.filter(ImageFilter.GaussianBlur(90)))
    img = _god_rays(img, S / 2, 300, c, count=22, alpha=40, seed=now.minute // 3)
    pos, size = ((S - 700) // 2, -86), 700
    img = _emblem_draw(img, size, pos, c)
    rnd = random.Random(now.hour * 60 + now.minute)
    for _ in range(7):
        img = _star(img, rnd.randint(150, 650), rnd.randint(30, 520), rnd.randint(8, 18))
    img = _star(img, S / 2, 22, 30)
    f = _fit("Poppins-Bold.ttf", hm, 520, 170)
    img = _crystal_text(img, hm, (S / 2, 662), f, c, depth=4, frost=0.08, tintk=0.34, glow=1.2, glow_r=24)
    return plain_text(img, date, (S / 2, 752), _font("Poppins-Medium.ttf", 34), (255, 255, 255, 235))


# ------------------------------------------------------------------ 8 more designs
def _sparkles(img, c, now, n=14, area=(40, 40, S - 40, S - 40), seed_extra=0, size=(7, 20)):
    rnd = random.Random(now.hour * 60 + now.minute + 7 + seed_extra)
    lay = _layer()
    d = ImageDraw.Draw(lay)
    for _ in range(n):
        x, y = rnd.randint(area[0], area[2]), rnd.randint(area[1], area[3])
        r = rnd.randint(*size)
        col = tint(c, 0.7) + (rnd.randint(170, 255),)
        d.polygon([(x, y - r), (x + r * 0.16, y - r * 0.16), (x + r, y), (x + r * 0.16, y + r * 0.16),
                   (x, y + r), (x - r * 0.16, y + r * 0.16), (x - r, y), (x - r * 0.16, y - r * 0.16)], fill=col)
    return _over(_over(img, lay.filter(ImageFilter.GaussianBlur(5))), lay)


def _crown(img, c, hm, date, now):
    """تاج شاهی: gold crown with jewels over a big glowing time."""
    img = _dim(img, 0.66)
    img = _god_rays(img, S / 2, 250, c, count=14, alpha=34, seed=now.minute // 5)
    cx, yb, w, h = S / 2, 330, 360, 200
    xs = [cx - w / 2, cx - w / 4, cx, cx + w / 4, cx + w / 2]
    ph = [0.76, 0.9, 1.0, 0.9, 0.76]
    pts = [(xs[0], yb)]
    for i, x in enumerate(xs):
        pts.append((x, yb - ph[i] * h))
        if i < 4:
            pts.append(((x + xs[i + 1]) / 2, yb - 0.48 * h))
    pts.append((xs[4], yb))
    ss = _SS()
    d = ss.d
    X = lambda p: (p[0] * 2, p[1] * 2)
    d.polygon([X(p) for p in pts], fill=(255, 255, 255, 255))
    d.rounded_rectangle(X((cx - w / 2 - 6, yb - 4)) + X((cx + w / 2 + 6, yb + 46)), radius=22, fill=(255, 255, 255, 255))
    for x, hh in zip(xs, ph):
        d.ellipse(X((x - 17, yb - hh * h - 20)) + X((x + 17, yb - hh * h + 14)), fill=(255, 255, 255, 255))
    shape = ss.done().split()[3]
    sh = Image.new("RGBA", (S, S), (0, 0, 0, 255))
    sh.putalpha(shape.filter(ImageFilter.GaussianBlur(14)).point(lambda p: int(p * 0.7)))
    img = _over(img, ImageChops.offset(sh, 0, 12))
    img = _glow_from_mask(img, shape, c, 26, 0.85, 1)
    fillg = _metal(tint(c, 0.92), c, shade(c, 0.55), int(yb - h), int(yb + 46)).convert("RGBA")
    fillg.putalpha(shape)
    img = _over(img, fillg)
    edge = ImageChops.subtract(shape.filter(ImageFilter.MaxFilter(5)), shape.filter(ImageFilter.MinFilter(3)))
    el = Image.new("RGBA", (S, S), shade(c, 0.6) + (255,))
    el.putalpha(edge.point(lambda p: int(p * 0.85)))
    img = _over(img, el)
    gems = _layer()
    gd = ImageDraw.Draw(gems)
    for x, hh in zip(xs, ph):
        gd.ellipse((x - 11, yb - hh * h - 14, x + 11, yb - hh * h + 8), fill=(255, 255, 255, 255))
        gd.ellipse((x - 6, yb - hh * h - 11, x + 2, yb - hh * h - 3), fill=tint(c, 0.95) + (255,))
    for i in range(5):
        gx = cx - w / 2 + 52 + i * (w - 104) / 4
        gd.polygon([(gx, yb + 6), (gx + 15, yb + 21), (gx, yb + 36), (gx - 15, yb + 21)], fill=(255, 255, 255, 235) if i == 2 else shade(c, 0.1) + (255,))
        gd.polygon([(gx, yb + 10), (gx + 7, yb + 18), (gx, yb + 24), (gx - 7, yb + 18)], fill=tint(c, 0.85) + (200,))
    img = _over(img, gems)
    img = _sparkles(img, c, now, 16, (60, 80, S - 60, 400))
    f = _fit("Poppins-Bold.ttf", hm, S * 0.82, 210)
    img = glow_text(img, hm, (S / 2, 545), f, c, radius=20, core=(255, 255, 255))
    ln = _layer()
    d = ImageDraw.Draw(ln)
    d.line([(S / 2 - 230, 676), (S / 2 - 24, 676)], fill=c + (255,), width=3)
    d.line([(S / 2 + 24, 676), (S / 2 + 230, 676)], fill=c + (255,), width=3)
    d.polygon([(S / 2, 664), (S / 2 + 12, 676), (S / 2, 688), (S / 2 - 12, 676)], fill=tint(c, 0.6) + (255,))
    img = _over(img, ln)
    return plain_text(img, date, (S / 2, 735), _font("Poppins-Medium.ttf", 42), (255, 255, 255, 235))


def _shield(img, c, hm, date, now):
    """سپر سلطنتی: crossed swords behind a metallic shield holding the time."""
    img = _dim(img, 0.66)
    cx = S / 2
    g1, g2, g3 = tint(c, 0.85), c, shade(c, 0.55)
    ss = _SS()
    d = ss.d
    X = lambda p: (p[0] * 2, p[1] * 2)
    for sgn in (-1, 1):                                       # crossed swords: blade tips above the corners, hilts below the sides
        a = sgn * 40
        ox, oy = cx, 440
        tip = _polar(ox, oy, 400, a)
        nx, ny = math.cos(math.radians(a)), math.sin(math.radians(a))
        base = _polar(ox, oy, -30, a)
        sh_ = _polar(ox, oy, 340, a)
        d.polygon([X((base[0] - nx * 14, base[1] - ny * 14)), X((sh_[0] - nx * 14, sh_[1] - ny * 14)), X(tip), X((sh_[0] + nx * 14, sh_[1] + ny * 14)), X((base[0] + nx * 14, base[1] + ny * 14))], fill=(255, 255, 255, 255))
        gx, gy = _polar(ox, oy, -60, a)
        d.line([X((gx - nx * 58, gy - ny * 58)), X((gx + nx * 58, gy + ny * 58))], fill=(255, 255, 255, 255), width=18)
        hx, hy = _polar(ox, oy, -250, a)
        d.line([X((gx, gy)), X((hx, hy))], fill=(255, 255, 255, 255), width=20)
        d.ellipse(X((hx - 20, hy - 20)) + X((hx + 20, hy + 20)), fill=(255, 255, 255, 255))
    swords = ss.done().split()[3]
    sw = _metal(g1, g2, g3, 80, 720).convert("RGBA")
    sw.putalpha(swords)
    img = _glow_from_mask(img, swords, c, 14, 0.6, 1)
    img = _over(img, sw)
    top = _cr([(cx - 250, 168), (cx - 125, 190), (cx, 166), (cx + 125, 190), (cx + 250, 168)], n=16)
    right = _cr([(cx + 250, 168), (cx + 262, 340), (cx + 196, 530), (cx, 716)], n=16)
    left = [(2 * cx - x, y) for x, y in reversed(right)]
    path = [(cx + (x - cx) * 0.9, 430 + (y - 430) * 0.9) for x, y in top + right[1:] + left[1:]]
    outer = path
    inner = [(cx + (x - cx) * 0.9, 430 + (y - 430) * 0.9) for x, y in path]
    m_out = Image.new("L", (S * 2, S * 2), 0)
    ImageDraw.Draw(m_out).polygon([(x * 2, y * 2) for x, y in path], fill=255)
    m_out = m_out.resize((S, S), Image.LANCZOS)
    m_in = Image.new("L", (S * 2, S * 2), 0)
    ImageDraw.Draw(m_in).polygon([(x * 2, y * 2) for x, y in inner], fill=255)
    m_in = m_in.resize((S, S), Image.LANCZOS)
    sh = Image.new("RGBA", (S, S), (0, 0, 0, 255))
    sh.putalpha(m_out.filter(ImageFilter.GaussianBlur(20)).point(lambda p: int(p * 0.8)))
    img = _over(img, ImageChops.offset(sh, 0, 16))
    img = _glow_from_mask(img, m_out, c, 24, 0.8, 1)
    rim = _metal(g1, g2, g3, 150, 712).convert("RGBA")
    rim.putalpha(m_out)
    img = _over(img, rim)
    face = _vgrad(S, S, shade(c, 0.55), shade(c, 0.93)).convert("RGBA")
    face.putalpha(m_in)
    img = _over(img, face)
    sheen = _layer()
    ImageDraw.Draw(sheen).polygon([(cx - 240, 160), (cx - 40, 160), (cx - 210, 560), (cx - 250, 470)], fill=(255, 255, 255, 34))
    sheen.putalpha(ImageChops.multiply(sheen.split()[3], m_in))
    img = _over(img, sheen)
    ln = _layer()
    ImageDraw.Draw(ln).line([(cx + (x - cx) * 0.8, 430 + (y - 430) * 0.8) for x, y in path + [path[0]]], fill=g2 + (150,), width=2, joint="curve")
    img = _over(img, ln)
    f = _fit("Poppins-Bold.ttf", hm, 330, 150)
    img = glow_text(img, hm, (cx, 400), f, c, radius=16, core=(255, 255, 255))
    st = _layer()
    d = ImageDraw.Draw(st)
    for i in range(5):
        x = cx - 80 + i * 40
        d.polygon([_polar(x, 282, 13 if j % 2 == 0 else 5.5, j * 36) for j in range(10)], fill=g1 + (255,))
    img = _over(img, st)
    return plain_text(img, date, (cx, 532), _font("Poppins-Medium.ttf", 40), (255, 255, 255, 235))


def _cinema(img, c, hm, date, now):
    """سینمایی: letterbox bars, film grain, REC dot and a movie-title time."""
    img = _dim(img, 0.3)
    tone = _layer()
    ImageDraw.Draw(tone).rectangle((0, 0, S, S), fill=c + (46,))
    img = _over(img, tone)
    grain = Image.effect_noise((S, S), 70).point(lambda p: int(abs(p - 128) * 0.28))
    gl = Image.new("RGBA", (S, S), (255, 255, 255, 255))
    gl.putalpha(grain)
    img = _over(img, gl)
    bars = _layer()
    d = ImageDraw.Draw(bars)
    d.rectangle((0, 0, S, 118), fill=(0, 0, 0, 255))
    d.rectangle((0, S - 150, S, S), fill=(0, 0, 0, 255))
    d.line([(0, 118), (S, 118)], fill=c + (255,), width=3)
    d.line([(0, S - 150), (S, S - 150)], fill=c + (255,), width=3)
    if now.minute % 2 == 0:
        d.ellipse((46, 44, 78, 76), fill=(255, 38, 54, 255))
    else:
        d.ellipse((46, 44, 78, 76), outline=(255, 38, 54, 255), width=4)
    img = _over(img, bars)
    img = plain_text(img, "REC", (96, 60), _font("Poppins-Bold.ttf", 34), (255, 255, 255, 240), anchor="lm", shadow=False)
    img = plain_text(img, "4K  ·  24 FPS", (S - 48, 60), _font("Poppins-Medium.ttf", 30), (255, 255, 255, 220), anchor="rm", shadow=False)
    lay = _layer()
    d = ImageDraw.Draw(lay)
    for (x, y, sx, sy) in ((60, 160, 1, 1), (S - 60, 160, -1, 1), (60, S - 190, 1, -1), (S - 60, S - 190, -1, -1)):
        d.line([(x, y), (x + 54 * sx, y)], fill=(255, 255, 255, 190), width=4)
        d.line([(x, y), (x, y + 54 * sy)], fill=(255, 255, 255, 190), width=4)
    img = _over(img, lay)
    f = _fit("Poppins-Bold.ttf", hm, S * 0.78, 230)
    img = glow_text(img, hm, (S / 2, 430), f, c, radius=22, core=(255, 244, 214), strength=1.1)
    img = plain_text(img, date, (S / 2, S - 76), _font("Poppins-Light.ttf", 50), (255, 255, 255, 235), shadow=False)
    return img


def _polaroid(img, c, hm, date, now):
    """پولاروید: the profile photo inside a tilted polaroid card, the time written below it."""
    photo = img.convert("RGB")
    bg = photo.filter(ImageFilter.GaussianBlur(26)).convert("RGBA")
    bg = _over(bg, Image.new("RGBA", (S, S), (0, 0, 0, 120)))
    bg = _over(bg, Image.new("RGBA", (S, S), c + (60,)))
    cw, ch = 520, 720
    card = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(card)
    x0, y0 = (S - cw) // 2, (S - ch) // 2
    d.rounded_rectangle((x0, y0, x0 + cw, y0 + ch), radius=14, fill=(248, 246, 240, 255))
    ph = cw - 56
    inner = ImageOps.fit(photo, (ph, ph), Image.LANCZOS)
    card.paste(inner, (x0 + 28, y0 + 32))
    d.rectangle((x0 + 28, y0 + 32, x0 + 28 + ph, y0 + 32 + ph), outline=(0, 0, 0, 55), width=2)
    d.rounded_rectangle((x0 + cw / 2 - 70, y0 - 14, x0 + cw / 2 + 70, y0 + 26), radius=4, fill=tint(c, 0.4) + (190,))
    f = _fit("Poppins-Medium.ttf", hm, cw - 140, 104)
    d.text((x0 + cw / 2, y0 + 32 + ph + 62), hm, font=f, fill=(34, 32, 38, 255), anchor="mm")
    d.text((x0 + cw / 2, y0 + ch - 34), date, font=_font("Poppins-Light.ttf", 30), fill=(110, 106, 116, 255), anchor="mm")
    d.ellipse((x0 + cw - 58, y0 + ch - 48, x0 + cw - 34, y0 + ch - 24), fill=c + (255,))
    card = card.rotate(-4, resample=Image.BICUBIC, center=(S / 2, S / 2))
    sh = Image.new("RGBA", (S, S), (0, 0, 0, 255))
    sh.putalpha(card.split()[3].filter(ImageFilter.GaussianBlur(20)).point(lambda p: int(p * 0.75)))
    bg = _over(bg, ImageChops.offset(sh, 8, 20))
    return _over(bg, card)


def _radar(img, c, hm, date, now):
    """رادار: rings, sweep beam that follows the minute hand, blips and the time in the middle."""
    img = _dim(img, 0.74)
    cx = cy = S / 2
    lay = _layer()
    d = ImageDraw.Draw(lay)
    for r in (92, 170, 248, 326):
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=c + (170 if r == 326 else 90,), width=3 if r == 326 else 2)
    d.line([(cx - 340, cy), (cx + 340, cy)], fill=c + (80,), width=2)
    d.line([(cx, cy - 340), (cx, cy + 340)], fill=c + (80,), width=2)
    for i in range(72):
        a = i * 5
        d.line([_polar(cx, cy, 326, a), _polar(cx, cy, 340 if i % 6 == 0 else 334, a)], fill=tint(c, 0.4) + (230,), width=3 if i % 6 == 0 else 2)
    img = _over(img, lay)
    theta = now.minute * 6 + now.second / 10
    sweep = _layer()
    sd = ImageDraw.Draw(sweep)
    for k in range(80):
        a0 = theta - k - 1
        sd.pieslice((cx - 326, cy - 326, cx + 326, cy + 326), a0 - 90, a0 - 90 + 1.6, fill=c + (int(150 * (1 - k / 80) ** 1.6),))
    img = _over(img, sweep.filter(ImageFilter.GaussianBlur(1.2)))
    line = _layer()
    ImageDraw.Draw(line).line([(cx, cy), _polar(cx, cy, 326, theta)], fill=tint(c, 0.7) + (255,), width=4)
    img = _glow_from_mask(img, line.split()[3], c, 9, 1.4, 1)
    img = _over(img, line)
    rnd = random.Random(now.hour * 60 + now.minute)
    blips = _layer()
    bd = ImageDraw.Draw(blips)
    for _ in range(7):
        r, a = rnd.randint(110, 310), rnd.randint(0, 359)
        x, y = _polar(cx, cy, r, a)
        bd.ellipse((x - 7, y - 7, x + 7, y + 7), fill=tint(c, 0.6) + (255,))
    img = _over(_over(img, blips.filter(ImageFilter.GaussianBlur(8))), blips)
    disc = _layer()
    ImageDraw.Draw(disc).ellipse((cx - 150, cy - 82, cx + 150, cy + 82), fill=(0, 0, 0, 175))
    img = _over(img, disc.filter(ImageFilter.GaussianBlur(10)))
    f = _fit("Poppins-Bold.ttf", hm, 290, 120)
    img = glow_text(img, hm, (cx, cy - 8), f, c, radius=14, core=(255, 255, 255))
    img = plain_text(img, date, (cx, cy + 56), _font("Poppins-Medium.ttf", 30), (255, 255, 255, 230), shadow=False)
    return img


def _hexa(img, c, hm, date, now):
    """شش‌ضلعی: honeycomb field with a glowing hexagon frame around the time."""
    img = _dim(img, 0.64)
    rnd = random.Random(now.hour * 60 + now.minute)
    size = 66
    lay = _layer()
    d = ImageDraw.Draw(lay)
    hexes = []
    for row in range(-1, 13):
        for col in range(-1, 11):
            x = col * size * 1.5 + 8
            y = row * size * math.sqrt(3) + (size * math.sqrt(3) / 2 if col % 2 else 0)
            hexes.append((x, y))
    for (x, y) in hexes:
        pts = [_polar(x, y, size - 3, 60 * i + 30) for i in range(6)]
        if rnd.random() < 0.13:
            d.polygon(pts, fill=c + (rnd.randint(40, 105),))
        d.line(pts + [pts[0]], fill=c + (70,), width=2)
    img = _over(img, lay)
    cx = cy = S / 2
    big = [_polar(cx, cy, 290, 60 * i + 30) for i in range(6)]
    mid = _layer()
    ImageDraw.Draw(mid).polygon(big, fill=(6, 6, 12, 205))
    img = _over(img, mid)
    gm = Image.new("L", (S, S), 0)
    ImageDraw.Draw(gm).line(big + [big[0]], fill=255, width=8, joint="curve")
    img = _glow_from_mask(img, gm, c, 16, 1.6, 2)
    ol = _layer()
    d = ImageDraw.Draw(ol)
    d.line(big + [big[0]], fill=tint(c, 0.75) + (255,), width=5, joint="curve")
    small = [_polar(cx, cy, 262, 60 * i + 30) for i in range(6)]
    d.line(small + [small[0]], fill=c + (130,), width=2)
    img = _over(img, ol)
    f = _fit("Poppins-Bold.ttf", hm, 400, 190)
    img = glow_text(img, hm, (cx, cy - 20), f, c, radius=18, core=(255, 255, 255))
    pill = _layer()
    ImageDraw.Draw(pill).rounded_rectangle((cx - 130, cy + 78, cx + 130, cy + 128), radius=25, fill=c + (255,))
    img = _over(img, pill)
    return plain_text(img, date, (cx, cy + 103), _font("Poppins-Bold.ttf", 34), (12, 12, 18, 255), shadow=False)


def _ember(img, c, hm, date, now):
    """آتش و خاکستر: fire glow rising from the bottom, flying sparks and a molten time."""
    img = _dim(img, 0.72)
    rnd = random.Random(now.hour * 60 + now.minute)
    glow = _layer()
    gd = ImageDraw.Draw(glow)
    for y in range(S):
        t = max(0.0, (y - 180) / (S - 180))
        gd.line([(0, y), (S, y)], fill=c + (int(235 * t ** 1.25),))
    img = _over(img, glow)
    fl = _layer()
    d = ImageDraw.Draw(fl)
    for i in range(16):
        x = i * S / 15 + rnd.randint(-22, 22)
        hgt = rnd.randint(250, 520)
        w = rnd.randint(42, 78)
        d.polygon([(x - w, S + 10), (x - w * 0.5, S - hgt * 0.45), (x + rnd.randint(-14, 14), S - hgt), (x + w * 0.45, S - hgt * 0.5), (x + w, S + 10)], fill=tint(c, 0.15) + (rnd.randint(110, 175),))
    img = _over(_over(img, fl.filter(ImageFilter.GaussianBlur(20))), fl.filter(ImageFilter.GaussianBlur(8)))
    core = _layer()
    d = ImageDraw.Draw(core)
    for i in range(16):
        x = i * S / 15 + rnd.randint(-22, 22)
        hgt = rnd.randint(130, 300)
        d.polygon([(x - 24, S + 10), (x + rnd.randint(-10, 10), S - hgt), (x + 24, S + 10)], fill=tint(c, 0.75) + (170,))
    img = _over(img, core.filter(ImageFilter.GaussianBlur(10)))
    sp = _layer()
    d = ImageDraw.Draw(sp)
    for _ in range(70):
        x, y = rnd.randint(0, S), rnd.randint(60, S - 40)
        r = rnd.choice((1.4, 2, 2.6, 3.4))
        k = rnd.randint(14, 46)
        d.line([(x, y), (x - k * 0.25, y + k)], fill=tint(c, 0.5) + (rnd.randint(60, 150),), width=1)
        d.ellipse((x - r, y - r, x + r, y + r), fill=tint(c, 0.75) + (rnd.randint(150, 255),))
    img = _over(_over(img, sp.filter(ImageFilter.GaussianBlur(3))), sp)
    f = _fit("Poppins-Bold.ttf", hm, S * 0.84, 250)
    pos = (S / 2, 300)
    m = _mask_text(hm, pos, f)
    img = _glow_from_mask(img, m, c, 26, 1.5, 2)
    hot = _metal((255, 255, 245), tint(c, 0.5), c, 170, 430).convert("RGBA")
    hot.putalpha(m)
    img = _over(img, hot)
    return plain_text(img, date, (S / 2, 500), _font("Poppins-Medium.ttf", 44), (255, 255, 255, 240))


def _hand(cx, cy, ang, length, w, tail):
    a = math.radians(ang - 90)
    ux, uy = math.cos(a), math.sin(a)
    nx, ny = -uy, ux
    return [(cx - ux * tail, cy - uy * tail), (cx + nx * w, cy + ny * w), (cx + ux * length * 0.82 + nx * w * 0.45, cy + uy * length * 0.82 + ny * w * 0.45),
            (cx + ux * length, cy + uy * length), (cx + ux * length * 0.82 - nx * w * 0.45, cy + uy * length * 0.82 - ny * w * 0.45), (cx - nx * w, cy - ny * w)]


def _roman(img, c, hm, date, now):
    """آنالوگ رومی لوکس: gold bezel, guilloche dial, roman numerals, real hands and a date window."""
    img = _dim(img, 0.7)
    cx = cy = S / 2 - 6
    g1, g2, g3 = tint(c, 0.85), c, shade(c, 0.55)
    sh = _layer()
    ImageDraw.Draw(sh).ellipse((cx - 352, cy - 336, cx + 352, cy + 368), fill=(0, 0, 0, 190))
    img = _over(img, sh.filter(ImageFilter.GaussianBlur(24)))
    bezel = _radial(346, g1, g3)
    bz = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bz.alpha_composite(bezel, (int(cx - 346), int(cy - 346)))
    img = _over(img, bz)
    dial = _radial(304, shade(c, 0.78), (5, 4, 7), 20)
    dz = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    dz.alpha_composite(dial, (int(cx - 304), int(cy - 304)))
    img = _over(img, dz)
    ss = _SS()
    d = ss.d
    X = lambda p: (p[0] * 2, p[1] * 2)
    for r in range(30, 300, 13):                                  # guilloche rings
        d.ellipse(X((cx - r, cy - r)) + X((cx + r, cy + r)), outline=g2 + (34,), width=2)
    for i in range(120):                                          # fine rays
        a = i * 3
        d.line([X(_polar(cx, cy, 60, a)), X(_polar(cx, cy, 296, a))], fill=g2 + (14,), width=2)
    d.ellipse(X((cx - 304, cy - 304)) + X((cx + 304, cy + 304)), outline=g1 + (255,), width=6)
    d.ellipse(X((cx - 330, cy - 330)) + X((cx + 330, cy + 330)), outline=g3 + (255,), width=4)
    for i in range(60):
        a = i * 6
        big = i % 5 == 0
        d.line([X(_polar(cx, cy, 290 if big else 296, a)), X(_polar(cx, cy, 262 if big else 284, a))], fill=g1 + (255,) if big else g2 + (210,), width=7 if big else 3)
    img = _over(img, ss.done())
    nums = ["XII", "I", "II", "III", "IIII", "V", "VI", "VII", "VIII", "IX", "X", "XI"]
    fn = _font("Poppins-Medium.ttf", 44)
    for i, t in enumerate(nums):
        x, y = _polar(cx, cy, 226, i * 30)
        img = plain_text(img, t, (x, y), fn, g1 + (255,), shadow=True)
    img = plain_text(img, hm, (cx, cy - 98), _font("Poppins-Bold.ttf", 54), (255, 255, 255, 235), shadow=True)
    hmx = _layer()
    hd = ImageDraw.Draw(hmx)
    hd.rounded_rectangle((cx - 66, cy + 108, cx + 66, cy + 152), radius=8, fill=(8, 8, 12, 235), outline=g2 + (255,), width=2)
    img = _over(img, hmx)
    img = plain_text(img, date, (cx, cy + 130), _font("Poppins-Medium.ttf", 26), (255, 255, 255, 240), shadow=False)
    hrs = (now.hour % 12 + now.minute / 60) * 30
    mins = now.minute * 6 + now.second / 10
    hand_l = _layer()
    ImageDraw.Draw(hand_l).polygon(_hand(cx, cy, hrs, 150, 11, 28), fill=(255, 255, 255, 255))
    ImageDraw.Draw(hand_l).polygon(_hand(cx, cy, mins, 232, 8.5, 34), fill=(255, 255, 255, 255))
    hm_mask = hand_l.split()[3]
    hs = Image.new("RGBA", (S, S), (0, 0, 0, 255))
    hs.putalpha(hm_mask.filter(ImageFilter.GaussianBlur(5)).point(lambda p: int(p * 0.7)))
    img = _over(img, ImageChops.offset(hs, 4, 7))
    gh = _metal(g1, g2, g3, int(cy - 240), int(cy + 40)).convert("RGBA")
    gh.putalpha(hm_mask)
    img = _over(img, gh)
    cap = _layer()
    cd = ImageDraw.Draw(cap)
    cd.ellipse((cx - 17, cy - 17, cx + 17, cy + 17), fill=g1 + (255,))
    cd.ellipse((cx - 8, cy - 8, cx + 8, cy + 8), fill=shade(c, 0.7) + (255,))
    return _over(img, cap)


STYLES += [("aurora", "شفق"), ("glitch", "گلیچ"), ("led", "ال‌ای‌دی"), ("synth", "سینت‌ویو"),
           ("gold", "طلایی لوکس"), ("matrix", "ماتریکس"), ("orbit", "مدار"), ("ecg", "ضربان"), ("flip", "فلیپ")]


_DRAW = {"neon": _neon, "stack": _stack, "ring": _ring, "analog": _analog, "glass": _glass,
         "hud": _hud, "capsule": _capsule, "minimal": _minimal,
         "aurora": _aurora, "glitch": _glitch, "led": _led, "synth": _synth, "gold": _gold,
         "matrix": _matrix, "orbit": _orbit, "ecg": _ecg, "flip": _flip}
STYLES += [("lionsun", "🦁 شیر و خورشید ۱"), ("lionsun2", "🦁 شیر و خورشید ۲"), ("crown", "تاج شاهی"), ("shield", "سپر سلطنتی"),
           ("cinema", "سینمایی"), ("polaroid", "پولاروید"), ("radar", "رادار"), ("hexa", "شش‌ضلعی"), ("ember", "آتش و خاکستر"),
           ("roman", "آنالوگ رومی لوکس")]
_DRAW.update({"lionsun": _lionsun, "lionsun2": _lionsun2, "crown": _crown, "shield": _shield, "cinema": _cinema,
              "polaroid": _polaroid, "radar": _radar, "hexa": _hexa, "ember": _ember, "roman": _roman})

# ------------------------------------------------------------------ the bloody red hand (دست قرمز)
@functools.lru_cache(maxsize=1)
def _hand_src():
    return Image.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "hand.png")).convert("RGBA")


@functools.lru_cache(maxsize=8)
def _hand_img(h):
    im = _hand_src()
    out = im.resize((max(1, round(im.width * h / im.height)), h), Image.LANCZOS)
    return out.filter(ImageFilter.UnsharpMask(radius=1.0, percent=50, threshold=2))


def _hand_draw(img, h, center, glow_col=(255, 24, 40), glow=1.0, alpha=1.0, glow_r=30):
    hd = _hand_img(h)
    full = _layer()
    full.paste(hd, (int(center[0] - hd.width / 2), int(center[1] - hd.height / 2)))
    a = full.split()[3]
    sh = Image.new("RGBA", (S, S), (0, 0, 0, 255))
    sh.putalpha(a.filter(ImageFilter.GaussianBlur(14)).point(lambda p: int(p * 0.7)))
    img = _over(img, ImageChops.offset(sh, 6, 12))
    if glow:
        img = _glow_from_mask(img, a, glow_col, glow_r, glow, 1)
    if alpha < 1:
        full.putalpha(a.point(lambda p: int(p * alpha)))
    img = _over(img, full)
    lit = ImageChops.subtract(a, ImageChops.offset(a, 3, 3)).filter(ImageFilter.GaussianBlur(0.7))   # wet highlight
    hl = Image.new("RGBA", (S, S), (255, 150, 150, 255))
    hl.putalpha(lit.point(lambda p: int(p * 0.55 * alpha)))
    return _over(img, hl)


def _blood_bg(img, c, strength=0.7):
    img = _dim(img, strength)
    halo = _layer()
    ImageDraw.Draw(halo).ellipse((S * .1, S * .1, S * .9, S * .9), fill=(150, 0, 14, 130))
    return _over(img, halo.filter(ImageFilter.GaussianBlur(120)))


def _hand1(img, c, hm, date, now):
    """دست قرمز ۱: big hand on the left, stacked glowing digits on the right."""
    img = _blood_bg(img, c)
    img = _hand_draw(img, 700, (232, 392))
    img = _palm_mark(img, (232, 392), 700)
    hh, mm = hm.split(":")
    f = _font("Poppins-Bold.ttf", 230)
    img = glow_text(img, hh, (600, 262), f, (255, 30, 50), radius=18, core=(255, 255, 255))
    img = glow_text(img, mm, (600, 490), f, (255, 30, 50), radius=18, core=(255, 215, 215))
    lay = _layer()
    d = ImageDraw.Draw(lay)
    d.rounded_rectangle((470, 372, 730, 380), radius=4, fill=(255, 40, 55, 255))
    img = _over(_glow_from_mask(img, lay.split()[3], (255, 30, 50), 8, 1.2, 1), lay)
    return plain_text(img, date, (600, 656), _font("Poppins-Medium.ttf", 34), (255, 205, 205, 235))


def _palm_mark(img, center, h, text="18.19"):
    """Fixed mark '18.19' engraved into the palm like it was scratched/painted in blood: dark crimson fill,
    wet rim-light, red glow and a few drips. Same on every render (seeded), so it always reads 18.19."""
    cx, cy = center[0] - 0.01 * h, center[1] + 0.065 * h
    f = _font("Poppins-Bold.ttf", int(h * 0.135))
    m = _mask_text(text, (cx, cy), f)
    bb = m.getbbox()
    if not bb:
        return img
    rnd = random.Random(1819)
    drips = Image.new("L", (S, S), 0)
    dd = ImageDraw.Draw(drips)
    n = len(text)
    for i in range(n):
        if i not in (1, 4):
            continue
        x = bb[0] + (bb[2] - bb[0]) * (i + 0.5) / n + (-2 if i == 1 else -10)
        ln = rnd.randint(int(h * 0.04), int(h * 0.11))
        w = max(5, int(h * 0.009))
        dd.rounded_rectangle((x - w, bb[3] - 2, x + w, bb[3] + ln), radius=w, fill=255)
        dd.ellipse((x - w * 1.5, bb[3] + ln - w, x + w * 1.5, bb[3] + ln + w * 2), fill=255)
    solid = ImageChops.lighter(m, drips)
    img = _glow_from_mask(img, solid, (255, 20, 40), 14, 0.9, 1)
    y0, y1 = bb[1], bb[3] + int(h * 0.1)
    strip = Image.new("RGB", (1, S))
    strip.putdata([(int(120 - 70 * t), int(6 - 6 * t), int(22 - 12 * t)) for t in
                   (min(1, max(0, (y - y0) / max(1, y1 - y0))) for y in range(S))])
    ol = strip.resize((S, S)).convert("RGBA")                           # deep crimson, darker towards the drips
    ol.putalpha(solid)
    img = _over(img, ol)
    rim = ImageChops.subtract(solid, ImageChops.offset(solid, 0, -3)).filter(ImageFilter.GaussianBlur(0.8))   # wet bottom rim
    r1 = Image.new("RGBA", (S, S), (255, 120, 130, 255))
    r1.putalpha(rim.point(lambda p: int(p * 0.95)))
    img = _over(img, r1)
    top = ImageChops.subtract(solid, ImageChops.offset(solid, 2, 3)).filter(ImageFilter.GaussianBlur(0.7))
    r2 = Image.new("RGBA", (S, S), (255, 235, 235, 255))
    r2.putalpha(top.point(lambda p: int(p * 0.75)))
    return _over(img, r2)


def _hand2(img, c, hm, date, now):
    """دست قرمز ۲: big hand with the fixed 18.19 mark on the palm, live clock underneath."""
    img = _blood_bg(img, c, 0.74)
    img = _hand_draw(img, 620, (S / 2 - 4, 320), glow=1.2, glow_r=36)
    img = _palm_mark(img, (S / 2 - 4, 320), 620)
    f = _fit("Poppins-Bold.ttf", hm, 470, 120)
    img = glow_text(img, hm, (S / 2, 694), f, (255, 30, 50), radius=16, core=(255, 255, 255))
    return plain_text(img, date, (S / 2, 766), _font("Poppins-Medium.ttf", 30), (255, 220, 220, 235))


def _hand3(img, c, hm, date, now):
    """دست قرمز ۳ (ترسناک): hand fading in from the side, dripping underline and a red scan-glow."""
    img = _blood_bg(img, c, 0.78)
    img = _hand_draw(img, 560, (640, 250), glow=1.4, alpha=0.9, glow_r=40)
    img = _palm_mark(img, (640, 250), 560)
    rnd = random.Random(now.hour * 60 + now.minute)
    lay = _layer()
    d = ImageDraw.Draw(lay)
    for _ in range(9):                                       # blood drops falling from the bottom bar
        x = rnd.randint(70, 520)
        ln = rnd.randint(18, 90)
        d.rounded_rectangle((x, 596, x + 9, 596 + ln), radius=4, fill=(170, 8, 24, 255))
        d.ellipse((x - 3, 596 + ln - 6, x + 12, 596 + ln + 9), fill=(170, 8, 24, 255))
    d.rounded_rectangle((60, 570, 540, 604), radius=14, fill=(190, 10, 28, 255))
    img = _over(img, lay)
    f = _fit("Poppins-Bold.ttf", hm, 470, 170)
    img = glow_text(img, hm, (300, 430), f, (255, 30, 50), radius=20, core=(255, 255, 255))
    return plain_text(img, date, (300, 536), _font("Poppins-Medium.ttf", 34), (255, 215, 215, 240))


STYLES += [("lionsun3", "🦁 شیر و خورشید ۳"), ("lionsun4", "🦁 شیر و خورشید ۴")]
_DRAW.update({"lionsun3": _lionsun3, "lionsun4": _lionsun4})
STYLES += [("hand1", "🩸 دست قرمز ۱"), ("hand2", "🩸 دست قرمز ۲"), ("hand3", "🩸 دست قرمز ۳")]
_DRAW.update({"hand1": _hand1, "hand2": _hand2, "hand3": _hand3})


# ------------------------------------------------------------------ 5 premium designs
def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _ramp(stops, t):
    """stops = [(pos, rgb), ...] sorted; returns the interpolated rgb at t."""
    t = min(1, max(0, t))
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        if p0 <= t <= p1:
            return _lerp(c0, c1, (t - p0) / max(1e-6, p1 - p0))
    return stops[-1][1]


def _starfield(img, n, seed, big=6):
    rnd = random.Random(seed)
    lay = _layer()
    d = ImageDraw.Draw(lay)
    glow = _layer()
    gd = ImageDraw.Draw(glow)
    for i in range(n):
        x, y = rnd.randint(0, S), rnd.randint(0, S)
        k = rnd.random() ** 2
        r = 0.7 + 1.6 * k
        tintc = rnd.choice(((255, 255, 255), (200, 220, 255), (255, 236, 210)))
        d.ellipse((x - r, y - r, x + r, y + r), fill=tintc + (int(90 + 165 * k),))
        if i < big:
            gd.ellipse((x - 5, y - 5, x + 5, y + 5), fill=tintc + (200,))
            d.line((x - 14, y, x + 14, y), fill=tintc + (120,), width=1)
            d.line((x, y - 14, x, y + 14), fill=tintc + (120,), width=1)
    img = _over(img, glow.filter(ImageFilter.GaussianBlur(5)))
    return _over(img, lay)


def _bhole(img, c, hm, date, now):
    """سیاه‌چاله: lensed accretion disk (hot inside, doppler-bright on one side), photon ring, star dust."""
    img = _dim(img, 0.86, vignette=False)
    img = _starfield(img, 190, now.hour * 60 + now.minute // 10)
    cx, cy, R = S / 2, 292, 112
    stops = [(0, tint(c, 0.95)), (0.22, tint(c, 0.55)), (0.55, c), (1, shade(c, 0.6))]
    disk = _layer()
    d = ImageDraw.Draw(disk)
    N = 34
    for i in range(N):
        t = i / (N - 1)
        rx = R * 1.18 + t * 340
        ry = rx * 0.2
        a = int(70 + 185 * (1 - t) ** 0.9)
        d.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), outline=_ramp(stops, t) + (a,), width=int(13 * (1 - t)) + 3)
    dop = Image.linear_gradient("L").rotate(90).resize((S, S))                    # bright on the approaching (left) side
    dop = dop.point(lambda p: int(110 + 145 * (p / 255)))
    disk.putalpha(ImageChops.multiply(disk.split()[3], dop))
    img = _over(img, disk.filter(ImageFilter.GaussianBlur(14)))
    img = _over(img, disk.filter(ImageFilter.GaussianBlur(2)))
    halo = _layer()                                                              # lensed back-of-disk wrapping the hole
    hd = ImageDraw.Draw(halo)
    for k, (w, a) in enumerate(((14, 230), (9, 170), (6, 110))):
        rr = R * (1.1 + 0.11 * k)
        hd.arc((cx - rr, cy - rr, cx + rr, cy + rr), 188, 352, fill=_ramp(stops, 0.15 + 0.25 * k) + (a,), width=w)
        hd.arc((cx - rr, cy - rr, cx + rr, cy + rr), 8, 172, fill=_ramp(stops, 0.2 + 0.25 * k) + (a // 3,), width=w // 2 + 1)
    img = _glow_from_mask(img, halo.split()[3], c, 18, 1.2, 1)
    img = _over(img, halo)
    hole = _layer()
    ImageDraw.Draw(hole).ellipse((cx - R, cy - R, cx + R, cy + R), fill=(0, 0, 0, 255))
    img = _over(img, hole)
    ring = _layer()
    ImageDraw.Draw(ring).ellipse((cx - R - 2, cy - R - 2, cx + R + 2, cy + R + 2), outline=tint(c, 0.9) + (255,), width=4)
    img = _glow_from_mask(img, ring.split()[3], c, 12, 1.8, 2)
    img = _over(img, ring)
    front = Image.new("L", (S, S), 0)                                            # the near half of the disk passes IN FRONT of the hole
    ImageDraw.Draw(front).rectangle((0, cy + 2, S, S), fill=255)
    fr = _layer()
    fr.paste(disk, (0, 0), front)
    img = _over(img, fr)
    f = _fit("Poppins-Bold.ttf", hm, 520, 190)
    img = glow_text(img, hm, (S / 2, 610), f, c, radius=22, core=(255, 255, 255), strength=1.4)
    lay = _layer()
    ImageDraw.Draw(lay).line([(S / 2 - 120, 706), (S / 2 + 120, 706)], fill=c + (255,), width=3)
    img = _over(_glow_from_mask(img, lay.split()[3], c, 6, 1.2, 1), lay)
    return plain_text(img, date, (S / 2, 748), _font("Poppins-Medium.ttf", 32), tint(c, 0.6) + (255,))


def _metal_fill(img, mask, c, light=0.55, dark=0.55, bevel=2):
    """Fill an L mask with brushed metal tinted by c: diagonal gradient + bevel light/dark edges."""
    g = Image.linear_gradient("L").rotate(45, resample=Image.BICUBIC).resize((S, S))
    top, bot = tint(c, light), shade(c, dark)
    mid = c
    strip = Image.new("RGB", (1, 256))
    strip.putdata([_ramp([(0, top), (0.5, mid), (0.55, shade(c, 0.25)), (1, bot)], i / 255) for i in range(256)])
    fill = Image.merge("RGB", [g] * 3)
    fill = Image.eval(g, lambda p: p)
    pal = strip.resize((256, 1))
    metal = Image.new("RGB", (S, S))
    lut = [strip.getpixel((0, i)) for i in range(256)]
    r = g.point([v[0] for v in lut])
    gg = g.point([v[1] for v in lut])
    b = g.point([v[2] for v in lut])
    metal = Image.merge("RGB", (r, gg, b)).convert("RGBA")
    metal.putalpha(mask)
    img = _over(img, metal)
    hi = ImageChops.subtract(mask, ImageChops.offset(mask, bevel, bevel)).filter(ImageFilter.GaussianBlur(0.8))
    lo = ImageChops.subtract(mask, ImageChops.offset(mask, -bevel, -bevel)).filter(ImageFilter.GaussianBlur(0.8))
    h = Image.new("RGBA", (S, S), (255, 255, 255, 255))
    h.putalpha(hi.point(lambda p: int(p * 0.85)))
    l = Image.new("RGBA", (S, S), (0, 0, 0, 255))
    l.putalpha(lo.point(lambda p: int(p * 0.7)))
    return _over(_over(img, h), l)


def _gear_mask(cx, cy, R, teeth, rot, holes=5, hub=0.2):
    ss = Image.new("L", (S * 2, S * 2), 0)
    d = ImageDraw.Draw(ss)
    pts = []
    n = teeth
    for i in range(n):
        a = rot + i * 360 / n
        w = 360 / n
        for off, rr in ((0.0, R * 0.86), (0.12, R * 0.86), (0.2, R), (0.5, R), (0.58, R * 0.86)):
            pts.append(tuple(v * 2 for v in _polar(cx, cy, rr, a + off * w)))
    d.polygon(pts, fill=255)
    d.ellipse(((cx - R * 0.66) * 2, (cy - R * 0.66) * 2, (cx + R * 0.66) * 2, (cy + R * 0.66) * 2), fill=0)         # rim ring
    d.ellipse(((cx - R * 0.7) * 2, (cy - R * 0.7) * 2, (cx + R * 0.7) * 2, (cy + R * 0.7) * 2), fill=255)
    d.ellipse(((cx - R * 0.58) * 2, (cy - R * 0.58) * 2, (cx + R * 0.58) * 2, (cy + R * 0.58) * 2), fill=0)
    for k in range(holes):                                                                                           # spokes
        a = rot * 1.7 + k * 360 / holes
        x1, y1 = _polar(cx, cy, R * hub, a)
        x2, y2 = _polar(cx, cy, R * 0.66, a)
        d.line([(x1 * 2, y1 * 2), (x2 * 2, y2 * 2)], fill=255, width=int(R * 0.12 * 2))
    d.ellipse(((cx - R * hub) * 2, (cy - R * hub) * 2, (cx + R * hub) * 2, (cy + R * hub) * 2), fill=255)
    d.ellipse(((cx - R * hub * 0.45) * 2, (cy - R * hub * 0.45) * 2, (cx + R * hub * 0.45) * 2, (cy + R * hub * 0.45) * 2), fill=0)
    return ss.resize((S, S), Image.LANCZOS)


def _skeleton(img, c, hm, date, now):
    """ساعت اسکلتی: real metal gears turning with the minutes, sapphire window with a minute-tick bezel."""
    base = _dim(img, 0.82)
    gold = _lerp(c, (255, 200, 90), 0.55)
    gears = ((120, 130, 150, 15, now.minute * 6), (700, 110, 118, 12, -now.minute * 7.5 + 15),
             (712, 700, 190, 18, now.minute * 4.8), (96, 706, 112, 11, -now.minute * 6.4 + 9),
             (400, 36, 70, 9, now.minute * 9))
    for x, y, R, t, rot in gears:
        m = _gear_mask(x, y, R, t, rot)
        sh = Image.new("RGBA", (S, S), (0, 0, 0, 255))
        sh.putalpha(m.filter(ImageFilter.GaussianBlur(9)).point(lambda p: int(p * 0.8)))
        base = _over(base, ImageChops.offset(sh, 6, 10))
        base = _metal_fill(base, m, gold)
    cx = cy = S / 2
    R = 262
    ss = _SS()
    ss.d.ellipse(ss.xy(cx - R - 22, cy - R - 22, cx + R + 22, cy + R + 22), fill=(0, 0, 0, 0))
    plate = Image.new("L", (S * 2, S * 2), 0)
    pd = ImageDraw.Draw(plate)
    pd.ellipse(((cx - R - 20) * 2, (cy - R - 20) * 2, (cx + R + 20) * 2, (cy + R + 20) * 2), fill=255)
    pd.ellipse(((cx - R + 14) * 2, (cy - R + 14) * 2, (cx + R - 14) * 2, (cy + R - 14) * 2), fill=0)
    bez = plate.resize((S, S), Image.LANCZOS)
    glass = _layer()
    ImageDraw.Draw(glass).ellipse((cx - R + 14, cy - R + 14, cx + R - 14, cy + R - 14), fill=(6, 8, 14, 226))
    sh2 = Image.new("RGBA", (S, S), (0, 0, 0, 255))
    sh2.putalpha(bez.filter(ImageFilter.GaussianBlur(10)).point(lambda p: int(p * 0.8)))
    base = _over(base, ImageChops.offset(sh2, 6, 12))
    base = _over(base, glass)
    base = _metal_fill(base, bez, _lerp(c, (230, 230, 240), 0.6), light=0.7, dark=0.5, bevel=3)
    tk = _SS()
    for i in range(60):
        on = i <= now.minute
        big = i % 5 == 0
        a, b = _polar(cx, cy, R - 30, i * 6), _polar(cx, cy, R - (58 if big else 46), i * 6)
        tk.d.line([(a[0] * 2, a[1] * 2), (b[0] * 2, b[1] * 2)], fill=(c + (255,)) if on else (255, 255, 255, 90), width=(9 if big else 5))
    ticks = tk.done()
    base = _glow_from_mask(base, ticks.split()[3].point(lambda p: p if p > 200 else 0), c, 8, 1.0, 1)
    base = _over(base, ticks)
    f = _fit("Poppins-Bold.ttf", hm, 380, 150)
    base = glow_text(base, hm, (cx, cy - 6), f, c, radius=16, core=(255, 255, 255), strength=1.1)
    base = plain_text(base, date, (cx, cy + 92), _font("Poppins-Medium.ttf", 30), tint(c, 0.6) + (255,))
    refl = _layer()                                                              # glass reflection
    ImageDraw.Draw(refl).pieslice((cx - R + 30, cy - R + 30, cx + R - 30, cy + R - 30), 200, 262, fill=(255, 255, 255, 34))
    base = _over(base, refl.filter(ImageFilter.GaussianBlur(3)))
    for k in range(4):                                                           # screws
        sx, sy = _polar(cx, cy, R + 4, 45 + 90 * k)
        base = _metal_fill(base, _mask_text("●", (sx, sy), _font("DejaVuSansMono-Bold.ttf", 18)), (200, 200, 210), bevel=1) if False else base
    return base


def _radeq(img, c, hm, date, now):
    """ایکولایزر دایره‌ای: 72 rounded bars around a glass clock, hue sweeping around the ring, reflection glow."""
    img = _dim(img, 0.82)
    cx = cy = S / 2
    rnd = random.Random(now.hour * 60 + now.minute)
    ph1, ph2 = rnd.random() * 6.28, rnd.random() * 6.28
    n = 72
    ss = _SS()
    glowl = _SS()
    for i in range(n):
        a = i * 360 / n
        lv = 0.5 + 0.5 * math.sin(i * 0.33 + ph1)
        lv *= 0.55 + 0.45 * math.sin(i * 0.11 + ph2)
        ln = 26 + 150 * abs(lv) + rnd.random() * 18
        col = _hue(c, (i / n - 0.5) * 0.28, s=0.8, v=1)
        p1, p2 = _polar(cx, cy, 176, a), _polar(cx, cy, 176 + ln, a)
        ss.d.line([(p1[0] * 2, p1[1] * 2), (p2[0] * 2, p2[1] * 2)], fill=col + (255,), width=18)
        for pp in (p1, p2):
            ss.d.ellipse(ss.xy(pp[0] - 4.5, pp[1] - 4.5, pp[0] + 4.5, pp[1] + 4.5), fill=col + (255,))
        q2 = _polar(cx, cy, 176 - 12 - ln * 0.28, a)                              # inner mirrored bar
        q1 = _polar(cx, cy, 164, a)
        glowl.d.line([(q1[0] * 2, q1[1] * 2), (q2[0] * 2, q2[1] * 2)], fill=col + (130,), width=12)
    bars = ss.done()
    img = _glow_from_mask(img, bars.split()[3], c, 16, 0.9, 1)
    img = _over(img, glowl.done())
    img = _over(img, bars)
    disc = _layer()
    d = ImageDraw.Draw(disc)
    d.ellipse((cx - 146, cy - 146, cx + 146, cy + 146), fill=(8, 10, 18, 215), outline=tint(c, 0.4) + (230,), width=4)
    img = _glow_from_mask(img, disc.split()[3], c, 10, 0.7, 1)
    img = _over(img, disc)
    f = _fit("Poppins-Bold.ttf", hm, 250, 100)
    img = plain_text(img, hm, (cx, cy - 8), f, (255, 255, 255, 255))
    img = plain_text(img, date, (cx, cy + 62), _font("Poppins-Medium.ttf", 24), tint(c, 0.6) + (255,), shadow=False)
    refl = _layer()
    ImageDraw.Draw(refl).pieslice((cx - 132, cy - 132, cx + 132, cy + 132), 200, 250, fill=(255, 255, 255, 30))
    return _over(img, refl)


def _horizon(img, c, hm, date, now):
    """افق سیاره: planet limb with layered atmosphere, cloud bands, city lights, sun flare with anamorphic streak."""
    img = _dim(img, 0.9, vignette=False)
    img = _starfield(img, 170, 7 + now.hour)
    cx, cy, R = S / 2 - 10, 1085, 650
    atm = _layer()
    ad = ImageDraw.Draw(atm)
    for k, (grow, a) in enumerate(((70, 40), (38, 70), (16, 120))):
        ad.ellipse((cx - R - grow, cy - R - grow, cx + R + grow, cy + R + grow), fill=c + (a,))
    img = _over(img, atm.filter(ImageFilter.GaussianBlur(26)))
    disc = Image.new("L", (S, S), 0)
    ImageDraw.Draw(disc).ellipse((cx - R, cy - R, cx + R, cy + R), fill=255)
    body = _vgrad(S, S, shade(c, 0.78), (3, 4, 9)).convert("RGBA")
    body.putalpha(disc)
    img = _over(img, body)
    clouds = Image.effect_noise((S, S), 70).filter(ImageFilter.GaussianBlur(16)).point(lambda p: max(0, min(255, (p - 118) * 4)))
    clouds = clouds.resize((S, S)).transform((S, S), Image.AFFINE, (1, 0.25, 0, 0, 1, 0))
    cl = Image.new("RGBA", (S, S), tint(c, 0.45) + (255,))
    fade = Image.linear_gradient("L").rotate(180).resize((S, S)).point(lambda p: int(p * 0.5 if p > 120 else p * 0.1))
    cl.putalpha(ImageChops.multiply(ImageChops.multiply(clouds, disc), fade).point(lambda p: int(p * 0.42)))
    img = _over(img, cl)
    rnd = random.Random(11)
    lights = _layer()
    ld = ImageDraw.Draw(lights)
    for _ in range(260):
        x, y = rnd.randint(40, S - 40), rnd.randint(int(cy - R + 150), S)
        if (x - cx) ** 2 + (y - cy) ** 2 < (R - 6) ** 2:
            r = rnd.choice((1, 1, 1.5, 2))
            ld.ellipse((x - r, y - r, x + r, y + r), fill=(255, 205, 120, rnd.randint(110, 230)))
    img = _over(_over(img, lights.filter(ImageFilter.GaussianBlur(2))), lights)
    rim = _layer()
    rd = ImageDraw.Draw(rim)
    for w, a in ((12, 70), (7, 130), (3, 255)):
        rd.arc((cx - R, cy - R, cx + R, cy + R), 238, 302, fill=tint(c, 0.75) + (a,), width=w)
    img = _glow_from_mask(img, rim.split()[3], c, 12, 1.6, 2)
    img = _over(img, rim)
    fx, fy = cx + 150, cy - math.sqrt(R * R - 150 ** 2)
    fl = _layer()
    fd = ImageDraw.Draw(fl)
    fd.ellipse((fx - 26, fy - 26, fx + 26, fy + 26), fill=(255, 255, 255, 255))
    img = _glow_from_mask(img, fl.split()[3], tint(c, 0.6), 36, 2.6, 2)
    streak = _layer()
    ImageDraw.Draw(streak).ellipse((fx - 330, fy - 5, fx + 330, fy + 5), fill=tint(c, 0.7) + (230,))
    img = _over(img, streak.filter(ImageFilter.GaussianBlur(4)))
    img = _over(img, fl)
    f = _fit("Poppins-Light.ttf", hm, 640, 250)
    img = glow_text(img, hm, (S / 2, 220), f, c, radius=26, core=(255, 255, 255), strength=0.8)
    return plain_text(img, date, (S / 2, 360), _font("Poppins-Medium.ttf", 36), tint(c, 0.65) + (255,))


STYLES += [("bhole", "🕳 سیاه‌چاله"), ("skeleton", "⚙️ ساعت اسکلتی"), ("radeq", "🎚 ایکولایزر دایره‌ای"),
           ("horizon", "🌍 افق سیاره")]
_DRAW.update({"bhole": _bhole, "skeleton": _skeleton, "radeq": _radeq, "horizon": _horizon})



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
    img = make_base(raw, (26, 28, 40) if ck == "dark" else col)
    if ck == "dark":
        img = _over(img, Image.new("RGBA", (S, S), (0, 0, 0, 150)))
    img = _DRAW[style_key(style)](img, col, now.strftime("%H:%M"), _date_text(now), now)
    img = img.convert("RGB")
    if OUT != S:                                            # high-res output: zooming in stays smooth
        img = img.resize((OUT, OUT), Image.LANCZOS).filter(ImageFilter.UnsharpMask(radius=1.2, percent=45, threshold=3))
    bio = io.BytesIO()
    img.save(bio, "JPEG", quality=96, subsampling=0, optimize=True)
    return bio.getvalue()
