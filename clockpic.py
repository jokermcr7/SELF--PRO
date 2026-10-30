"""Clock-on-profile-photo renderer (Pillow).

render(raw_base_photo_bytes | None, style, color, now) -> JPEG bytes (800x800)

27 designs x 7 colors. The base photo is the user's own profile picture (or a gradient if there is none);
every design is drawn ON TOP of it, so the photo stays visible.
"""
import functools
import io
import math
import os
from datetime import datetime

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps

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

def _lion_build(size, ss=2):
    """Heraldic walking lion facing left (raised paw + scimitar, curly mane, S-tail) as an L mask. 100x100 design grid."""
    N = size * ss; k = N / 100.0
    m = Image.new("L", (N, N), 0); d = ImageDraw.Draw(m)
    P = lambda x, y: (x * k, y * k)
    def disc(x, y, r, v=255):
        d.ellipse((P(x - r, y - r) + P(x + r, y + r)), fill=v)
    def tube(pts, w0, w1, n=14, v=255):
        path = _cr(pts, n=n)
        for i, (x, y) in enumerate(path):
            disc(x, y, (w0 + (w1 - w0) * i / max(len(path) - 1, 1)) / 2, v)
    def poly(pts, n=12, v=255):
        d.polygon([P(*p) for p in _cr(pts, n=n, closed=True)], fill=v)
    def cut(pts, w=0.7, n=10):
        d.line([P(*p) for p in _cr(pts, n=n)], fill=0, width=max(1, int(w * k)), joint="curve")

    # hind legs (far then near)
    tube([(80, 54), (77, 66), (83, 77), (80, 91)], 10, 3.4); poly([(74, 91), (86, 91), (88, 94.5), (74, 94.5)], n=4)
    tube([(70, 54), (66, 67), (71, 77), (67, 91)], 11.5, 3.8); poly([(60, 91), (72, 91), (74, 94.5), (60, 94.5)], n=4)
    # standing front leg
    tube([(45, 58), (47, 72), (44, 83), (41, 91)], 9.5, 3.6); poly([(34, 91), (46, 91), (48, 94.5), (33, 94.5)], n=4)
    # torso
    poly([(36, 42), (46, 40), (58, 43.5), (70, 41), (80, 42), (86, 48), (85, 58), (78, 63.5), (68, 59), (58, 57.5), (48, 63), (40, 64), (35, 55)])
    # tail: S curve rising above the back with a flame tuft
    tube([(84, 46), (92, 45), (96, 36), (92, 27), (87, 24)], 3.2, 2.0, n=10)
    d.polygon([P(*p) for p in ((84.5, 27), (82, 18), (86.5, 20), (86, 10), (90.5, 17.5), (93.5, 11), (93, 21), (91, 29))], fill=255)
    disc(87.5, 27, 2.6)
    # raised front leg
    tube([(45, 55), (38, 64), (28, 64), (22, 57)], 9.5, 5.2)
    disc(21.5, 56, 5.0)
    # --- curly mane: ring of bumps around the head centre, open on the face side
    hx, hy = 37, 34
    for ring, (rad, br, a0, a1, cnt) in enumerate(((13.5, 6.4, -150, 110, 13), (18.6, 4.6, -138, 92, 11))):
        for i in range(cnt):
            a = math.radians(a0 + (a1 - a0) * i / (cnt - 1) + (4 if ring else 0))
            disc(hx + rad * math.cos(a), hy + 1.05 * rad * math.sin(a), br + 0.6 * math.sin(i * 2.1))
    disc(hx, hy, 13.5)
    # back locks trailing over the shoulder
    for (x, y, r) in ((53, 33, 4.2), (59, 36, 3.6), (64, 40, 3.0), (49, 46, 5), (54, 49, 3.8), (45, 53, 4.6)):
        disc(x, y, r)
    # --- head profile (facing left)
    poly([(28.0, 24.5), (23.5, 28.0), (19.0, 33.0), (14.5, 36.5), (12.5, 40.0), (14.5, 43.5), (19.0, 45.0), (23.0, 48.0), (29.0, 47.5), (34.0, 42.0), (34.0, 29.0)], n=10)
    poly([(30.0, 18.0), (35.0, 15.5), (38.0, 21.0), (33.5, 24.5)], n=6)               # ear
    # --- engraved details
    cut([(28.5, 26), (31.5, 32), (32, 40), (29, 47)], 0.75)                  # face / mane line
    cut([(13.5, 41), (18, 42.4), (22, 41)], 0.5)                             # mouth
    cut([(42, 24), (44, 29), (43, 34)], 0.5)                                 # mane locks
    cut([(46, 46), (43, 54), (45, 61)], 0.7)                                 # shoulder
    cut([(66, 46), (71, 54), (69, 62)], 0.8)                                 # thigh
    cut([(78, 44), (82, 51)], 0.6)
    # --- scimitar, hilt in the raised paw
    blade = _cr([(21.5, 53.5), (14, 42), (9, 28), (8, 15), (12, 3.5)], n=12)
    L = len(blade); lf, rt = [], []
    for i, (x, y) in enumerate(blade):
        w = 1.5 * (1 - (i / (L - 1)) ** 1.9) + 0.12
        lf.append(P(x - w, y)); rt.append(P(x + w, y))
    d.polygon(lf + rt[::-1], fill=255)
    d.line([P(15.5, 54), P(27.5, 57.5)], fill=255, width=int(1.8 * k))
    disc(25.5, 60.2, 2.0)
    return m.resize((size, size), Image.LANCZOS)



LION_EYE = (24.5, 36.4)                                    # eye position on the 100x100 lion grid


@functools.lru_cache(maxsize=8)
def _lion_mask(size):
    return _lion_build(size)


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


def _sun_layer(cx, cy, r, c, face=True, rays=30):
    """Sun with alternating straight and wavy rays and a calm face (like the classic emblem). Full-size RGBA layer."""
    ss = _SS()
    d = ss.d
    X = lambda p: (p[0] * 2, p[1] * 2)
    light, mid, dark = tint(c, 0.62), tint(c, 0.12), shade(c, 0.45)
    for i in range(rays):
        a = i * 360 / rays
        if i % 2 == 0:                                       # long pointed ray
            pts = [_polar(cx, cy, r * 1.05, a - 4.2), _polar(cx, cy, r * 2.0, a), _polar(cx, cy, r * 1.05, a + 4.2)]
            d.polygon([X(p) for p in pts], fill=light + (255,))
            d.polygon([X(p) for p in (_polar(cx, cy, r * 1.05, a), _polar(cx, cy, r * 2.0, a), _polar(cx, cy, r * 1.05, a + 4.2))],
                      fill=mid + (255,))
        else:                                                # wavy flame ray
            left, right = [], []
            for j in range(13):
                t = j / 12
                rad = r * 1.05 + t * r * 0.78
                ang = a + 3.2 * math.sin(t * math.pi * 2.0)
                w = 4.4 * (1 - t) ** 0.7 + 0.3
                left.append(X(_polar(cx, cy, rad, ang - w)))
                right.append(X(_polar(cx, cy, rad, ang + w)))
            d.polygon(left + right[::-1], fill=mid + (255,))
    d.ellipse(X((cx - r * 1.14, cy - r * 1.14)) + X((cx + r * 1.14, cy + r * 1.14)), fill=dark + (255,))
    layer = ss.done()
    disc = _radial(int(r), tint(c, 0.78), c)
    layer.alpha_composite(disc, (int(cx - r), int(cy - r)))
    ss2 = _SS()
    d = ss2.d
    d.ellipse(X((cx - r, cy - r)) + X((cx + r, cy + r)), outline=dark + (255,), width=5)
    if face:
        f = shade(c, 0.55) + (255,)
        for sx in (-1, 1):                                   # closed eyes + brows
            ex = cx + sx * r * 0.36
            d.arc(X((ex - r * 0.17, cy - r * 0.2)) + X((ex + r * 0.17, cy + r * 0.04)), 20, 160, fill=f, width=6)
            d.arc(X((ex - r * 0.24, cy - r * 0.36)) + X((ex + r * 0.24, cy - r * 0.08)), 200, 340, fill=f, width=5)
        d.line([X((cx, cy - r * 0.1)), X((cx - r * 0.05, cy + r * 0.16)), X((cx + r * 0.07, cy + r * 0.2))], fill=f, width=5)
        d.arc(X((cx - r * 0.3, cy + r * 0.2)) + X((cx + r * 0.3, cy + r * 0.52)), 25, 155, fill=f, width=6)
    layer.alpha_composite(ss2.done())
    return layer


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


def _paste_lion(img, size, pos, c, fill_img, rim_col, eye_col=None, rim=11, glow=1.6, outline=None):
    """Draw the lion silhouette filled with fill_img, a glowing rim light and a glowing eye."""
    lion = _lion_mask(size)
    mask = Image.new("L", (S, S), 0)
    mask.paste(lion, pos)
    edge = ImageChops.subtract(mask.filter(ImageFilter.MaxFilter(rim)), mask)
    img = _glow_from_mask(img, edge, rim_col, 7, glow, 2)
    if outline:
        ol = Image.new("RGBA", (S, S), outline + (255,))
        ol.putalpha(ImageChops.subtract(mask.filter(ImageFilter.MaxFilter(7)), mask))
        img = _over(img, ol)
    lay = fill_img.convert("RGBA")
    lay.putalpha(mask)
    img = _over(img, lay)
    if not outline:
        rl = Image.new("RGBA", (S, S), rim_col + (255,))
        rl.putalpha(edge.point(lambda p: int(p * 0.9)))
        img = _over(img, rl)
    ex, ey = pos[0] + LION_EYE[0] * size / 100, pos[1] + LION_EYE[1] * size / 100
    em = Image.new("L", (S, S), 0)
    ImageDraw.Draw(em).ellipse((ex - 9, ey - 6, ex + 9, ey + 6), fill=255)
    img = _glow_from_mask(img, em, eye_col or rim_col, 8, 2.0, 2)
    el = _layer()
    ImageDraw.Draw(el).ellipse((ex - 5, ey - 3.5, ex + 5, ey + 3.5), fill=(255, 255, 255, 255))
    return _over(img, el)


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


def _lionsun(img, c, hm, date, now):
    """شیر و خورشید ۱: dark lion silhouette with a glowing rim in front of a blazing sun, light beams and embers."""
    img = _dim(img, 0.68)
    cx, cy, r = 540, 214, 112
    halo = _layer()
    ImageDraw.Draw(halo).ellipse((cx - 340, cy - 340, cx + 340, cy + 340), fill=c + (135,))
    img = _over(img, halo.filter(ImageFilter.GaussianBlur(105)))
    img = _god_rays(img, cx, cy, c, seed=now.minute // 5)
    sun = _sun_layer(cx, cy, r, c)
    img = _glow_from_mask(img, sun.split()[3], c, 28, 0.95, 1)
    img = _over(img, sun)
    ground = _layer()
    ImageDraw.Draw(ground).ellipse((120, 548, 700, 596), fill=c + (150,))
    img = _over(img, ground.filter(ImageFilter.GaussianBlur(20)))
    body = _metal(shade(c, 0.86), shade(c, 0.93), (4, 3, 5), 60, 560)
    img = _paste_lion(img, 520, (110, 44), c, body, tint(c, 0.55))
    img = _embers(img, c, now, 40, (60, 40, 760, 600))
    f = _fit("Poppins-Bold.ttf", hm, S * 0.8, 170)
    img = glow_text(img, hm, (S / 2, 655), f, c, radius=18, core=(255, 255, 255))
    return plain_text(img, date, (S / 2, 752), _font("Poppins-Medium.ttf", 38), (255, 255, 255, 235))


def _lionsun2(img, c, hm, date, now):
    """شیر و خورشید ۲: royal gold medallion - sunburst, ornamental rings, golden lion and a time ribbon."""
    img = _dim(img, 0.7)
    cx, cy, R = S // 2, 338, 318
    g1, g2, g3 = tint(c, 0.78), c, shade(c, 0.5)
    ss = _SS()
    d = ss.d
    X = lambda p: (p[0] * 2, p[1] * 2)
    d.ellipse(X((cx - R, cy - R)) + X((cx + R, cy + R)), fill=shade(c, 0.9) + (255,))
    for i in range(36):                                      # sunburst wedges inside the disc
        a = i * 10
        d.polygon([X((cx, cy)), X(_polar(cx, cy, R - 24, a - 2.6)), X(_polar(cx, cy, R - 24, a + 2.6))], fill=shade(c, 0.62) + (255,) if i % 2 == 0 else shade(c, 0.78) + (255,))
    disc = ss.done()
    clip = Image.new("L", (S, S), 0)
    ImageDraw.Draw(clip).ellipse((cx - R, cy - R, cx + R, cy + R), fill=255)
    sh = _layer()
    ImageDraw.Draw(sh).ellipse((cx - R - 6, cy - R + 14, cx + R + 6, cy + R + 14), fill=(0, 0, 0, 170))
    img = _over(img, sh.filter(ImageFilter.GaussianBlur(22)))
    img = _over(img, disc)
    # rings + tick marks
    ss = _SS()
    d = ss.d
    for rr, w, col in ((R, 7, g2), (R - 14, 3, g1), (R - 40, 3, g2), (R - 56, 2, g3)):
        d.ellipse(X((cx - rr, cy - rr)) + X((cx + rr, cy + rr)), outline=col + (255,), width=w * 2)
    for i in range(72):
        a = i * 5
        p1, p2 = _polar(cx, cy, R - 40, a), _polar(cx, cy, R - 52 if i % 2 else R - 56, a)
        d.line([X(p1), X(p2)], fill=g1 + (255,), width=4)
    for i in range(12):
        a = i * 30
        d.ellipse(X((_polar(cx, cy, R - 27, a)[0] - 6, _polar(cx, cy, R - 27, a)[1] - 6)) + X((_polar(cx, cy, R - 27, a)[0] + 6, _polar(cx, cy, R - 27, a)[1] + 6)), fill=g1 + (255,))
    img = _over(img, ss.done())
    # sun (behind the lion) + golden lion
    sun = _sun_layer(cx + 96, cy - 118, 92, c, rays=24)
    inner = Image.new("L", (S, S), 0)
    ImageDraw.Draw(inner).ellipse((cx - R + 62, cy - R + 62, cx + R - 62, cy + R - 62), fill=255)
    sun.putalpha(ImageChops.multiply(sun.split()[3], inner))
    img = _glow_from_mask(img, sun.split()[3], c, 22, 0.7, 1)
    img = _over(img, sun)
    gold = _metal(tint(c, 0.9), g2, shade(c, 0.62), 80, 520)
    img = _paste_lion(img, 440, (cx - 232, 92), c, gold, g1, rim=9, glow=0.55, outline=shade(c, 0.82))
    # ribbon with the time
    rb = _layer()
    d = ImageDraw.Draw(rb)
    y0, y1 = 578, 700
    d.polygon([(70, y0 + 24), (150, y0 + 24), (150, y1 - 10), (70, y1 - 10), (104, (y0 + y1) / 2 + 6)], fill=g3 + (255,))
    d.polygon([(S - 70, y0 + 24), (S - 150, y0 + 24), (S - 150, y1 - 10), (S - 70, y1 - 10), (S - 104, (y0 + y1) / 2 + 6)], fill=g3 + (255,))
    img = _over(img, rb)
    shb = Image.new("RGBA", (S, S), (0, 0, 0, 255))
    shb.putalpha(_round_mask((130, y0 + 12, S - 130, y1 + 12), 26).filter(ImageFilter.GaussianBlur(14)).point(lambda p: int(p * 0.7)))
    img = _over(img, shb)
    plate = _metal(g1, g2, g3, y0, y1).convert("RGBA")
    plate.putalpha(_round_mask((130, y0, S - 130, y1 - 4), 26))
    img = _over(img, plate)
    ol = _layer()
    ImageDraw.Draw(ol).rounded_rectangle((130, y0, S - 130, y1 - 4), radius=26, outline=tint(c, 0.9) + (230,), width=3)
    img = _over(img, ol)
    f = _fit("Poppins-Bold.ttf", hm, S * 0.46, 118)
    img = plain_text(img, hm, (S / 2, (y0 + y1) / 2 - 4), f, shade(c, 0.78) + (255,), shadow=False)
    return plain_text(img, date, (S / 2, 752), _font("Poppins-Medium.ttf", 38), (255, 255, 255, 235))


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
