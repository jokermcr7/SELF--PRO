"""Clock-on-profile-photo renderer (Pillow).

render(_ignored, style, color, now) -> JPEG bytes (1280x1280)

The photo is ALWAYS the bundled artwork assets/poster_art.jpg (the red / black "18 | 19" brush circle) - the old
designs and the per-user base photo are gone. 3 designs x 9 colors; the chosen color re-colors the red brush parts of
the artwork and drives the digits / ticks / hands (the default red leaves the artwork untouched).
"""
import colorsys
import functools
import io
import math
import os
import random
from datetime import datetime

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

S = 800
OUT = 1280           # uploaded size (profile photos are stored/zoomed up to this)
_HERE = os.path.dirname(os.path.abspath(__file__))
_FD = os.path.join(_HERE, "assets", "fonts")
_SYS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf", "Arial.ttf"]
ART_FILE = os.path.join(_HERE, "assets", "poster_art.jpg")
ART_SIZE = 640
ART_C = (320, 303)       # centre of the brush circle inside the 640x640 artwork
ART_R = 256              # radius of that circle
PAPER = (232, 223, 209)  # paper colour of the artwork

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
}
COLOR_ORDER = list(COLORS)

# key -> Persian name (the ONLY three designs)
STYLES = [("light", "🖼 پوستر روشن"), ("dark", "🌌 پوستر تاریک"), ("analog", "🕰 ساعت آنالوگ")]


# ------------------------------------------------------------------ small helpers
@functools.lru_cache(maxsize=128)
def _font(name, size):
    for p in (os.path.join(_FD, name), *_SYS):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()

def tint(c, k):        # mix with white
    return tuple(int(v + (255 - v) * k) for v in c)

def shade(c, k):       # mix with black
    return tuple(int(v * (1 - k)) for v in c)



def _layer():
    return Image.new("RGBA", (S, S), (0, 0, 0, 0))


def _over(img, layer):
    return Image.alpha_composite(img, layer)


def _glow_from_mask(img, mask, col, radius=16, strength=1.7, passes=2):
    g = mask.filter(ImageFilter.GaussianBlur(radius)).point(lambda p: min(255, int(p * strength)))
    for _ in range(passes):
        layer = Image.new("RGBA", (S, S), col + (255,))
        layer.putalpha(g)
        img = _over(img, layer)
    return img


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


# ------------------------------------------------------------------ brush digits / text helpers
def _dtxt(date):
    return date.replace("/", " / ")


def _track(img, text, pos, font, fill, track=3.0):
    """Letter-spaced text centred on pos."""
    widths = [font.getlength(ch) for ch in text]
    x = pos[0] - (sum(widths) + track * (len(text) - 1)) / 2
    layer = _layer()
    d = ImageDraw.Draw(layer)
    for ch, w in zip(text, widths):
        d.text((x, pos[1]), ch, font=font, fill=fill, anchor="lm")
        x += w + track
    return _over(img, layer)


def _vgrad2(top, bot):
    g = Image.linear_gradient("L").resize((S, S))
    return Image.composite(Image.new("RGB", (S, S), bot), Image.new("RGB", (S, S), top), g).convert("RGBA")


def _paint(img, mask, col, alpha=1.0):
    layer = Image.new("RGBA", (S, S), tuple(col)[:3] + (255,))
    layer.putalpha(mask if alpha >= 1 else mask.point(lambda p: int(p * alpha)))
    return _over(img, layer)


def _drop(img, mask, off=(5, 8), blur=8, opacity=0.32, col=(0, 0, 0)):
    return _paint(img, ImageChops.offset(mask, off[0], off[1]).filter(ImageFilter.GaussianBlur(blur)), col, opacity)


def _brush(mask, seed, rough=1.0, shear=0.16, cy=S / 2):
    """Clean glyph mask -> dry-brush look: italic lean, heavier stroke, wavy + ragged edges, bristle scratches.
    Ink is only ever removed/added right next to the glyph (no stray specks anywhere else)."""
    rnd = random.Random(seed)
    if shear:
        mask = mask.transform((S, S), Image.AFFINE, (1, shear, -shear * cy, 0, 1, 0), resample=Image.BICUBIC)
    box = mask.getbbox()
    if not box:
        return mask
    m = mask.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(1.3 * rough))     # a touch bolder, like a wet brush
    fine = Image.effect_noise((S, S), 40 * rough)
    low = Image.effect_noise((S // 5, S // 5), 62 * rough).resize((S, S), Image.BICUBIC)          # slow waviness of the edge
    m = ImageChops.add(m, ImageChops.add(fine, low, 2.0, 0), 1.0, -128)                           # m + noise - 128
    m = m.point(lambda p: 255 if p > 150 else (0 if p < 104 else int((p - 104) * 255 / 46)))
    m = m.filter(ImageFilter.GaussianBlur(0.8))
    m = ImageChops.multiply(m, mask.filter(ImageFilter.MaxFilter(9)))
    streak = Image.new("L", (S, S), 255)
    d = ImageDraw.Draw(streak)
    x0, y0, x1, y1 = box
    for _ in range(int((x1 - x0) * (y1 - y0) / 650 * rough)):
        y, x, ln = rnd.randint(y0, y1), rnd.randint(x0 - 30, x1), rnd.randint(24, 140)
        d.line([(x, y), (x + ln, y + rnd.randint(-2, 2))], fill=rnd.randint(40, 170), width=rnd.choice((1, 1, 2)))
    return ImageChops.multiply(m, streak)


def _time_masks(hm, size, cx, cy, gap=0.30):
    """-> (hours_mask, colon_mask, minutes_mask, total_width): brush digits with a hand-drawn two-dot colon."""
    hh, mm = hm.split(":")
    f = _font("Poppins-Bold.ttf", size)
    wh, wm = f.getlength(hh), f.getlength(mm)
    g, wc = size * gap, size * 0.17
    x = cx - (wh + wm + wc + 2 * g) / 2
    hmask, mmask, cmask = (Image.new("L", (S, S), 0) for _ in range(3))
    ImageDraw.Draw(hmask).text((x, cy), hh, font=f, fill=255, anchor="lm")
    xc = x + wh + g + wc / 2
    dc, r = ImageDraw.Draw(cmask), size * 0.062
    for dy in (-0.17, 0.17):
        dc.ellipse((xc - r, cy + dy * size - r, xc + r, cy + dy * size + r), fill=255)
    ImageDraw.Draw(mmask).text((x + wh + 2 * g + wc, cy), mm, font=f, fill=255, anchor="lm")
    return hmask, cmask, mmask, wh + wm + wc + 2 * g


def _poster_digits(seed, hm, size, cx, cy, gap=0.30, shear=0.16, rough=1.0):
    h, c, m, w = _time_masks(hm, size, cx, cy, gap)
    return (_brush(h, seed, rough, shear, cy), _brush(c, seed + 1, rough * 0.6, shear, cy),
            _brush(m, seed + 2, rough, shear, cy), w)


def _hand_poly(cx, cy, ang, length, w_base, w_tip, tail):
    a = math.radians(ang - 90)
    ux, uy, px, py = math.cos(a), math.sin(a), -math.sin(a), math.cos(a)
    return [(cx - ux * tail + px * w_base / 2, cy - uy * tail + py * w_base / 2),
            (cx + ux * length + px * w_tip / 2, cy + uy * length + py * w_tip / 2),
            (cx + ux * length - px * w_tip / 2, cy + uy * length - py * w_tip / 2),
            (cx - ux * tail - px * w_base / 2, cy - uy * tail - py * w_base / 2)]


# ------------------------------------------------------------------ the artwork
@functools.lru_cache(maxsize=1)
def _art():
    try:
        return Image.open(ART_FILE).convert("RGB")
    except OSError:                                         # file missing: a plain stand-in so the clock still works
        im = Image.new("RGB", (ART_SIZE, ART_SIZE), PAPER)
        d = ImageDraw.Draw(im)
        d.ellipse((ART_C[0] - ART_R, ART_C[1] - ART_R, ART_C[0] + ART_R, ART_C[1] + ART_R), outline=(176, 20, 32), width=36)
        return im


@functools.lru_cache(maxsize=16)
def _art_for(col):
    """The artwork with its strongly saturated (red) brush parts re-coloured to the accent; red itself = untouched."""
    art = _art()
    if col == COLORS["red"][2]:
        return art
    h, s, _v = colorsys.rgb_to_hsv(*(x / 255 for x in col))
    hh, sat, val = art.convert("HSV").split()
    m = sat.point(lambda p: 0 if p < 70 else min(255, (p - 70) * 5))
    k = min(1.0, s / 0.85)
    hh = Image.composite(Image.new("L", art.size, int(h * 255) % 256), hh, m)
    sat = Image.composite(sat.point(lambda p: int(p * k)), sat, m)
    val = Image.composite(val.point(lambda p: min(255, int(p * 1.14))), val, m)
    return Image.merge("HSV", (hh, sat, val)).convert("RGB")


def _ink(c):
    """The accent color made dark enough to read on paper (light accents like white / gold get darker)."""
    lum = 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]
    return shade(c, 0.2 + 0.4 * min(1.0, max(0.0, (lum - 120) / 110)))


def _paper_canvas():
    return Image.new("RGBA", (S, S), PAPER + (255,))


def _place_art(canvas, col, cx, cy, ring_diameter, feather=26):
    """Paste the artwork so that its brush circle has the given diameter, centred on (cx, cy); the edges melt into the paper."""
    k = ring_diameter / (2 * ART_R)
    size = max(16, round(ART_SIZE * k))
    art = _art_for(col).resize((size, size), Image.LANCZOS).convert("RGBA")
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rectangle((feather, feather, size - feather, size - feather), fill=255)
    canvas.paste(art, (round(cx - ART_C[0] * k), round(cy - ART_C[1] * k)), m.filter(ImageFilter.GaussianBlur(feather / 3.2)))
    return canvas


# ------------------------------------------------------------------ design 1: light poster
def _light(c, hm, date, now):
    """پوستر روشن: the artwork on paper, big brush digits below (hours in the accent color, minutes black) + Jalali date."""
    img = _place_art(_paper_canvas(), c, S / 2, S * 0.372, S * 0.57)
    hmk, cmk, mmk, _w = _poster_digits(11, hm, 134, S / 2, S * 0.797, 0.34)
    img = _drop(img, ImageChops.lighter(ImageChops.lighter(hmk, cmk), mmk), (5, 8), 8, 0.28)
    img = _paint(img, hmk, _ink(c))
    img = _paint(img, cmk, (66, 64, 66))
    img = _paint(img, mmk, (22, 22, 26))
    return _track(img, _dtxt(date), (S / 2, S * 0.935), _font("Poppins-Medium.ttf", 24), (86, 82, 78, 255), 3.4)


# ------------------------------------------------------------------ design 2: dark poster
def _dark(c, hm, date, now):
    """پوستر تاریک: the artwork inside a circle with a glowing accent ring on a dark stage; white hours, glowing minutes."""
    stage = _vgrad2((17, 17, 21), (5, 5, 8))
    cx, cy, R = S / 2, S * 0.40, S * 0.285
    disc = _place_art(_paper_canvas(), c, cx, cy, 2 * R * 0.93)
    d2, x0, y0 = int(R * 2), int(cx - R), int(cy - R)
    cm = Image.new("L", (d2 * 4, d2 * 4), 0)
    ImageDraw.Draw(cm).ellipse((0, 0, d2 * 4 - 1, d2 * 4 - 1), fill=255)
    stage.paste(disc.crop((x0, y0, x0 + d2, y0 + d2)), (x0, y0), cm.resize((d2, d2), Image.LANCZOS))
    ss = _SS()
    ss.d.ellipse(ss.xy(cx - R - 7, cy - R - 7, cx + R + 7, cy + R + 7), outline=shade(c, 0.12) + (255,), width=9 * 2)
    ss.d.ellipse(ss.xy(cx - R - 30, cy - R - 30, cx + R + 30, cy + R + 30), outline=(205, 205, 212, 125), width=2 * 2)
    rings = ss.done()
    img = _glow_from_mask(stage, rings.split()[3].point(lambda p: 255 if p > 200 else 0), c, 9, 0.55, 1)
    img = _over(img, rings)
    hmk, cmk, mmk, _w = _poster_digits(21, hm, 112, S / 2, S * 0.842, 0.34, 0.14, 0.9)
    img = _paint(img, hmk, (246, 242, 236))
    img = _paint(img, cmk, (226, 222, 216))
    img = _glow_from_mask(img, mmk, c, 11, 1.1, 1)
    img = _paint(img, mmk, tint(c, 0.10))
    return _track(img, _dtxt(date), (S / 2, S * 0.935), _font("Poppins-Medium.ttf", 22), (176, 172, 170, 255), 3.4)


# ------------------------------------------------------------------ design 3: analog clock on the artwork
def _analog(c, hm, date, now):
    """ساعت آنالوگ: minute ticks all around (hour ticks in the accent color) and a real analog clock over the artwork."""
    img = _place_art(_paper_canvas(), c, S / 2, S / 2, S * 0.70)
    cx = cy = S / 2
    R = S * 0.488
    ink = (28, 28, 32)
    ss = _SS()
    for i in range(60):
        major = i % 5 == 0
        a, b = _polar(cx, cy, R, i * 6), _polar(cx, cy, R - (34 if major else 17), i * 6)
        ss.d.line(ss.xy(*a, *b), fill=(_ink(c) + (255,)) if major else (ink + (175,)), width=(7 if major else 3) * 2)
    img = _over(img, ss.done())
    ha, ma = (now.hour % 12 + now.minute / 60) * 30, now.minute * 6
    hands = _SS()
    for ang, ln, wb, wt, tl, col in ((ha, S * 0.235, 21, 10, 30, shade(c, 0.05) + (255,)),
                                     (ma, S * 0.37, 13, 4, 46, (250, 248, 244, 255))):
        pts = [(x * 2, y * 2) for x, y in _hand_poly(cx, cy, ang, ln, wb, wt, tl)]
        hands.d.polygon(pts, fill=col)
        hands.d.line(pts + [pts[0]], fill=(20, 20, 24, 255), width=3, joint="curve")          # thin outline: visible on paper too
    hands.d.ellipse(hands.xy(cx - 15, cy - 15, cx + 15, cy + 15), fill=(250, 248, 244, 255), outline=(20, 20, 24, 255), width=3)
    hands.d.ellipse(hands.xy(cx - 8, cy - 8, cx + 8, cy + 8), fill=shade(c, 0.1) + (255,))
    hl = hands.done()
    sh = Image.new("RGBA", (S, S), (0, 0, 0, 255))
    sh.putalpha(ImageChops.offset(hl.split()[3], 4, 7).filter(ImageFilter.GaussianBlur(6)).point(lambda p: int(p * 0.45)))
    return _over(_over(img, sh), hl)


_DRAW = {"light": _light, "dark": _dark, "analog": _analog}


def style_key(v):
    """Stored value (key or old numeric index) -> one of the three designs; anything unknown -> the first design."""
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
    """`raw` (a base photo) is accepted for backwards compatibility and ignored: the base is always the artwork."""
    now = now or datetime.now()
    col = COLORS[color_key(color)][2]
    img = _DRAW[style_key(style)](col, now.strftime("%H:%M"), _date_text(now), now)
    img = img.convert("RGB")
    if OUT != S:                                            # high-res output: zooming in stays smooth
        img = img.resize((OUT, OUT), Image.LANCZOS).filter(ImageFilter.UnsharpMask(radius=1.2, percent=45, threshold=3))
    bio = io.BytesIO()
    img.save(bio, "JPEG", quality=96, subsampling=0, optimize=True)
    return bio.getvalue()
