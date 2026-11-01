"""🎡 Daily lucky wheel (گردونه شانس) for every user of the manager bot.

* One free spin per Tehran calendar day (resets at 00:00 Tehran time). The spin is claimed atomically in the
  database first, so double clicks / two bot processes can never pay twice.
* Prizes 20 40 60 80 100 120 140 200 💎 with falling odds (20 is the most likely, 200 the rarest).
  Change them with the env var  WHEEL_WEIGHTS="20:34,40:24,60:15,80:10,100:7,120:5,140:3.5,200:1.5".
* The prize is drawn with the OS random generator, then a real spinning animation (GIF, drawn with Pillow) is built
  that slows down and stops exactly on that prize under the pointer. After it ends the animation is replaced by a
  result card.
"""
import asyncio
import html
import io
import logging
import math
import os
import random
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps
from telethon import events

import botapi
import db
import utils
from botapi import GREEN, RED, BLUE, btn, kb

log = logging.getLogger("wheel")
TZ = ZoneInfo("Asia/Tehran")
BASE = os.path.dirname(os.path.abspath(__file__))
_sys = random.SystemRandom()

# order of the 8 slices around the wheel (clockwise from the top): big prizes are spread out
ORDER = [20, 100, 40, 140, 60, 200, 80, 120]
DEFAULT_WEIGHTS = {20: 34, 40: 24, 60: 15, 80: 10, 100: 7, 120: 5, 140: 3.5, 200: 1.5}


def _weights():
    w = dict(DEFAULT_WEIGHTS)
    raw = os.getenv("WHEEL_WEIGHTS", "").strip()
    if raw:
        try:
            new = {int(a): float(b) for a, b in (x.split(":") for x in raw.split(",") if x.strip())}
            if set(new) == set(ORDER) and all(v > 0 for v in new.values()):
                w = new
            else:
                log.warning("WHEEL_WEIGHTS must list exactly %s with positive numbers; using defaults", ORDER)
        except Exception:
            log.warning("WHEEL_WEIGHTS is not valid (use 20:34,40:24,...); using defaults")
    return w


WEIGHTS = _weights()


def odds():
    """[(prize, percent)] sorted by prize."""
    tot = sum(WEIGHTS.values())
    return [(v, WEIGHTS[v] * 100 / tot) for v in sorted(WEIGHTS)]


def average_prize():
    tot = sum(WEIGHTS.values())
    return sum(v * w for v, w in WEIGHTS.items()) / tot


def draw_prize():
    """-> (slice index on the wheel, prize). Uses the OS random source."""
    prize = _sys.choices(ORDER, weights=[WEIGHTS[v] for v in ORDER], k=1)[0]
    return ORDER.index(prize), prize


# ------------------------------------------------------------------ Tehran day helpers
def day_start(now=None):
    n = datetime.fromtimestamp(now or time.time(), TZ)
    return n.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def left_text(now=None):
    """Time until the next 00:00 Tehran, e.g. '5 ساعت و 12 دقیقه'."""
    now = now or time.time()
    s = int(day_start(now) + 86400 - now)
    h, m = s // 3600, s % 3600 // 60
    return f"{h} ساعت و {m} دقیقه" if h else f"{max(m, 1)} دقیقه"


# ------------------------------------------------------------------ drawing
CW, CH = 480, 560                 # canvas
CX, CY = 240, 314                 # wheel centre
GIF_SCALE = 0.7                   # the animation is sent smaller (slow mobile data!) than the result card
R_OUT, R_IN, R_HUB = 206, 178, 40
N = len(ORDER)
SEG = 360 / N
COLORS = [(226, 52, 72), (245, 160, 70), (36, 160, 148), (124, 52, 200), (236, 198, 90), (36, 124, 214),
          (216, 40, 124), (60, 180, 110)]
GOLD_D, GOLD_L = (150, 98, 14), (255, 226, 130)

_cache = {}


def _font(size):
    for name in ("Poppins-Bold.ttf", "DejaVuSansMono-Bold.ttf"):
        try:
            return ImageFont.truetype(os.path.join(BASE, "assets", "fonts", name), size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _gem(d, cx, cy, r, alpha=255):
    """Small diamond icon centred at (cx, cy) with radius r."""
    pts = [(cx - r, cy - r * 0.35), (cx - r * 0.55, cy - r * 0.95), (cx + r * 0.55, cy - r * 0.95),
           (cx + r, cy - r * 0.35), (cx, cy + r)]
    d.polygon(pts, fill=(150, 225, 255, alpha), outline=(255, 255, 255, alpha))
    d.polygon([pts[0], pts[1], (cx, cy - r * 0.35)], fill=(210, 245, 255, alpha))
    d.polygon([pts[3], pts[2], (cx, cy - r * 0.35)], fill=(90, 170, 235, alpha))
    d.polygon([pts[0], pts[3], pts[4]], fill=(70, 150, 225, alpha))
    d.line([pts[0], (cx, cy - r * 0.35), pts[3]], fill=(255, 255, 255, alpha), width=max(1, int(r / 8)))


def _bg(title=True):
    im = Image.new("RGB", (CW, CH))
    d = ImageDraw.Draw(im)
    for y in range(CH):
        t = y / CH
        d.line([(0, y), (CW, y)], fill=(int(10 + 30 * t), int(8 + 6 * t), int(30 + 34 * t)))
    glow = Image.new("RGB", (CW, CH), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse([CX - R_OUT - 8, CY - R_OUT - 8, CX + R_OUT + 8, CY + R_OUT + 8], fill=(255, 168, 40))
    glow = glow.filter(ImageFilter.GaussianBlur(40)).point(lambda v: int(v * 0.42))
    im = ImageChops.add(im, glow)
    rnd = random.Random(11)
    d = ImageDraw.Draw(im)
    for _ in range(80):
        x, y, r = rnd.randrange(CW), rnd.randrange(CH), rnd.choice((1, 1, 2))
        d.ellipse([x - r, y - r, x + r, y + r], fill=rnd.choice(((255, 232, 160), (255, 255, 255), (255, 190, 100))))
    if not title:
        return im
    f = _font(33)
    title, gap = "LUCKY WHEEL", 5
    w = sum(d.textlength(c, font=f) + gap for c in title) - gap
    x0 = (CW - w) / 2
    lay = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lay)
    x = x0
    for c in title:
        ld.text((x, 18), c, font=f, fill=(255, 218, 110, 255))
        x += ld.textlength(c, font=f) + gap
    glow2 = lay.filter(ImageFilter.GaussianBlur(7))
    im = Image.alpha_composite(im.convert("RGBA"), glow2)
    im = Image.alpha_composite(im, lay).convert("RGB")
    d = ImageDraw.Draw(im)
    d.line([(CW / 2 - 70, 62), (CW / 2 + 70, 62)], fill=(255, 200, 80), width=2)
    return im


def _wheel_base():
    S = 2
    R = R_IN - 2
    size = (R * 2 + 8) * S
    c = size // 2
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for i in range(N):
        d.pieslice([c - R * S, c - R * S, c + R * S, c + R * S], i * SEG - 90, (i + 1) * SEG - 90, fill=COLORS[i])
    for i in range(N):                                                    # separators
        a = math.radians(i * SEG - 90)
        d.line([(c, c), (c + math.cos(a) * R * S, c + math.sin(a) * R * S)], fill=(255, 255, 255, 190), width=3 * S)
    # glossy highlight: bright centre fading to the rim
    rad = ImageOps.invert(Image.radial_gradient("L").resize((size, size), Image.BICUBIC)).point(lambda v: int(v * 0.32))
    circle = Image.new("L", (size, size), 0)
    ImageDraw.Draw(circle).ellipse([c - R * S, c - R * S, c + R * S, c + R * S], fill=255)
    hl = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    hl.putalpha(ImageChops.multiply(rad, circle))
    im = Image.alpha_composite(im, hl)
    d = ImageDraw.Draw(im)
    f = _font(50 * S)
    for i, val in enumerate(ORDER):
        mid = i * SEG + SEG / 2                                           # clockwise from the top
        rr = R * S * 0.62
        px, py = c + math.sin(math.radians(mid)) * rr, c - math.cos(math.radians(mid)) * rr
        t = Image.new("RGBA", (190 * S, 96 * S), (0, 0, 0, 0))
        td = ImageDraw.Draw(t)
        txt = str(val)
        tw = td.textlength(txt, font=f)
        td.text(((190 * S - tw) / 2, 14 * S), txt, font=f, fill=(255, 255, 255, 255), stroke_width=3 * S, stroke_fill=(20, 10, 40, 210))
        t = t.rotate(-mid, resample=Image.BICUBIC, expand=True)
        im.alpha_composite(t, (int(px - t.width / 2), int(py - t.height / 2)))
        gr = R * S * 0.86                                                 # little gem near the rim
        gx, gy = c + math.sin(math.radians(mid)) * gr, c - math.cos(math.radians(mid)) * gr
        g = Image.new("RGBA", (40 * S, 40 * S), (0, 0, 0, 0))
        _gem(ImageDraw.Draw(g), 20 * S, 20 * S, 9 * S)
        g = g.rotate(-mid, resample=Image.BICUBIC, expand=True)
        im.alpha_composite(g, (int(gx - g.width / 2), int(gy - g.height / 2)))
    return im.resize((size // S, size // S), Image.LANCZOS)


def _overlays():
    """Static layers on top of the turning wheel: gold rim with chasing bulbs (2 variants), hub, pointer."""
    S = 2
    big = (CW * S, CH * S)
    base = Image.new("RGBA", big, (0, 0, 0, 0))
    d = ImageDraw.Draw(base)
    for r in range(R_OUT * S, R_IN * S - 1, -1):                         # beveled gold ring
        t = (r - R_IN * S) / ((R_OUT - R_IN) * S)
        k = 0.5 - 0.5 * math.cos(t * math.pi * 2)
        d.ellipse([CX * S - r, CY * S - r, CX * S + r, CY * S + r], fill=_mix(GOLD_D, GOLD_L, k) + (255,))
    d.ellipse([CX * S - R_IN * S, CY * S - R_IN * S, CX * S + R_IN * S, CY * S + R_IN * S], fill=(0, 0, 0, 0))
    d.ellipse([CX * S - R_IN * S, CY * S - R_IN * S, CX * S + R_IN * S, CY * S + R_IN * S], outline=(90, 55, 5, 255), width=3 * S)
    d.ellipse([CX * S - R_OUT * S, CY * S - R_OUT * S, CX * S + R_OUT * S, CY * S + R_OUT * S], outline=(90, 55, 5, 255), width=3 * S)
    # hub
    for r in range(R_HUB * S, 0, -1):
        k = 1 - r / (R_HUB * S)
        d.ellipse([CX * S - r, CY * S - r, CX * S + r, CY * S + r], fill=_mix(GOLD_D, GOLD_L, 0.25 + 0.75 * k) + (255,))
    d.ellipse([CX * S - R_HUB * S, CY * S - R_HUB * S, CX * S + R_HUB * S, CY * S + R_HUB * S], outline=(90, 55, 5, 255), width=3 * S)
    d.ellipse([CX * S - 27 * S, CY * S - 27 * S, CX * S + 27 * S, CY * S + 27 * S], fill=(30, 18, 60, 255))
    _gem(d, CX * S, CY * S + 2 * S, 17 * S)
    # pointer (soft shadow first)
    tip = (CX * S, (CY - R_OUT + 40) * S)
    poly = [(CX * S - 27 * S, (CY - R_OUT - 22) * S), (CX * S + 27 * S, (CY - R_OUT - 22) * S), tip]
    sh = Image.new("RGBA", big, (0, 0, 0, 0))
    ImageDraw.Draw(sh).polygon([(x + 3 * S, y + 6 * S) for x, y in poly], fill=(0, 0, 0, 170))
    sh = sh.filter(ImageFilter.GaussianBlur(5 * S))
    base = Image.alpha_composite(sh, base)
    d = ImageDraw.Draw(base)
    d.polygon(poly, fill=(255, 214, 80, 255), outline=(110, 60, 0, 255))
    d.polygon([poly[0], (CX * S, poly[0][1]), tip], fill=(255, 238, 160, 255))
    d.line([poly[0], poly[1], tip, poly[0]], fill=(110, 60, 0, 255), width=3 * S)
    d.ellipse([CX * S - 7 * S, (CY - R_OUT - 14) * S - 7 * S, CX * S + 7 * S, (CY - R_OUT - 14) * S + 7 * S], fill=(210, 40, 60, 255), outline=(110, 60, 0, 255), width=2 * S)
    base = base.resize((CW, CH), Image.LANCZOS)
    out = []
    for variant in (0, 1):
        lay = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
        gl = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
        ld, gd = ImageDraw.Draw(lay), ImageDraw.Draw(gl)
        rr = (R_IN + R_OUT) / 2
        for i in range(24):
            a = math.radians(i * 15)
            bx, by = CX + math.sin(a) * rr, CY - math.cos(a) * rr
            on = (i + variant) % 2 == 0
            if on:
                gd.ellipse([bx - 11, by - 11, bx + 11, by + 11], fill=(255, 240, 150, 150))
            ld.ellipse([bx - 6, by - 6, bx + 6, by + 6], fill=(255, 250, 200, 255) if on else (140, 92, 18, 255),
                       outline=(90, 55, 5, 255))
        gl = gl.filter(ImageFilter.GaussianBlur(4))
        out.append(Image.alpha_composite(Image.alpha_composite(base, gl), lay))
    return out


def _assets():
    if not _cache:
        _cache["bg"] = _bg()
        _cache["bg_plain"] = _bg(False)
        _cache["wheel"] = _wheel_base()
        _cache["ov"] = _overlays()
    return _cache


def _compose(angle, variant, blur_step=0.0, plain=False):
    a = _assets()
    w = a["wheel"]

    def rot(x):
        return w.rotate(-x, resample=Image.BICUBIC)               # PIL rotates counter-clockwise; the wheel turns clockwise
    img = rot(angle)
    if blur_step > 10:                                              # cheap motion blur at high speed
        img = Image.blend(img, rot(angle - blur_step / 3), 0.5)
        img = Image.blend(img, rot(angle - 2 * blur_step / 3), 1 / 3)
    fr = a["bg_plain" if plain else "bg"].convert("RGBA")
    fr.alpha_composite(img, (CX - w.width // 2, CY - w.height // 2))
    fr.alpha_composite(a["ov"][variant])
    return fr


def _ease(t):
    return 0.62 * (1 - (1 - t) ** 3) + 0.38 * (t * t * (3 - 2 * t))


def spin_gif(idx):
    """Animated GIF of the wheel that stops exactly on slice `idx` -> (gif bytes, total duration in ms)."""
    rnd = random.Random()
    centre = idx * SEG + SEG / 2 + rnd.uniform(-13, 13)              # never exactly in the middle
    total = rnd.choice((4, 5)) * 360 + (360 - centre) % 360
    n = 58
    angles = [total * _ease(i / (n - 1)) for i in range(n)]
    frames, durs = [], []
    footer = _font(17)
    for i, ang in enumerate(angles):
        step = ang - (angles[i - 1] if i else ang - 6)
        fr = _compose(ang, i % 2, step)
        d = ImageDraw.Draw(fr)
        txt = "DAILY FREE SPIN"
        d.text(((CW - d.textlength(txt, font=footer)) / 2, CH - 34), txt, font=footer, fill=(255, 235, 170, 235))
        fr = fr.convert("RGB").resize((int(CW * GIF_SCALE), int(CH * GIF_SCALE)), Image.LANCZOS)
        frames.append(fr.quantize(colors=64, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE))
        durs.append(46 if i < 42 else int(46 + (i - 41) * 4.4))
    frames.append(frames[-1])
    durs.append(1600)
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=durs, loop=0, disposal=1)
    return buf.getvalue(), sum(durs)


def result_png(idx, prize):
    """The finished wheel (pointer on the prize) with a big '+N' banner on top."""
    total = (360 - (idx * SEG + SEG / 2)) % 360
    fr = _compose(total, 0, plain=True)
    d = ImageDraw.Draw(fr)
    rnd = random.Random(prize)
    for _ in range(110):                                              # confetti (only outside the wheel)
        x = rnd.choice((rnd.randrange(0, 120), rnd.randrange(CW - 120, CW)))
        y = rnd.randrange(0, CH)
        if math.hypot(x - CX, y - CY) < R_OUT + 10:
            continue
        c = rnd.choice(((255, 90, 110), (255, 210, 90), (90, 220, 255), (150, 255, 160), (255, 140, 240)))
        ang = rnd.uniform(0, math.pi)
        w, h = rnd.randint(4, 8), rnd.randint(8, 14)
        pts = [(x + math.cos(ang) * dx - math.sin(ang) * dy, y + math.sin(ang) * dx + math.cos(ang) * dy)
               for dx, dy in ((-w, -h), (w, -h), (w, h), (-w, h))]
        d.polygon(pts, fill=c + (255,))
    big, small = _font(54), _font(15)
    txt = f"+{prize}"
    tw = d.textlength(txt, font=big)
    x0 = (CW - tw - 50) / 2
    lay = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lay)
    ld.text((x0 + 50, 2), txt, font=big, fill=(255, 220, 110, 255), stroke_width=2, stroke_fill=(110, 60, 0, 255))
    _gem(ld, x0 + 22, 38, 22)
    ld.text(((CW - ld.textlength("D I A M O N D S", font=small)) / 2, 66), "D I A M O N D S", font=small, fill=(255, 245, 210, 255))
    fr = Image.alpha_composite(fr, lay.filter(ImageFilter.GaussianBlur(8)))
    fr = Image.alpha_composite(fr, lay)
    buf = io.BytesIO()
    fr.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ------------------------------------------------------------------ bot side
def esc(s):
    return html.escape(str(s), quote=False)


def panel_text(available, name=""):
    lines = "\n".join(f"💎 <b>{v}</b> الماس ← {p:.1f}٪" for v, p in odds())
    status = ("✅ <b>امروز هنوز نچرخوندی!</b> دکمه‌ی پایین رو بزن 👇" if available else
              f"⏳ امروز چرخوندی؛ گردونه‌ی بعدی <b>{left_text()}</b> دیگه باز میشه (۰۰:۰۰ به وقت تهران)")
    return (f"<blockquote>🎡 گردونه شانس روزانه</blockquote>\n"
            f"هر روز <b>یک‌بار رایگان</b> گردونه رو بچرخون و تا <b>۲۰۰ 💎</b> برنده شو! 🎁\n\n"
            f"🎯 <b>جوایز و شانس هر کدوم:</b>\n{lines}\n"
            f"📊 میانگین جایزه: ≈ <b>{average_prize():.0f}</b> الماس\n\n{status}")


def panel_markup(available):
    first = (btn("🎡 بچرخون!", "w:s", GREEN) if available else btn("⏳ فردا دوباره بیا", "w:n", RED))
    return kb([[first], [btn("بازگشت", "m:menu", RED)]])


_sem = None


def register(bot, admin_ids, send_menu):
    global _sem
    _sem = asyncio.Semaphore(2)                                          # at most 2 animations rendered at once
    asyncio.get_event_loop().run_in_executor(None, _assets)              # warm-up: first spinner does not wait

    async def is_available(uid):
        return uid in admin_ids or await db.wheel_last(uid) < day_start()

    async def show_panel(chat, uid, message_id=None):
        av = await is_available(uid)
        text, markup = panel_text(av), panel_markup(av)
        if message_id:
            try:
                return await botapi.edit_any({"chat_id": chat, "message_id": message_id}, text, markup)
            except Exception:
                log.exception("wheel panel edit failed; sending a new one")
        return await botapi.send_banner(chat, text, markup)

    @bot.on(events.NewMessage(func=lambda e: e.is_private and not e.out))
    async def on_text(e):
        t = utils.to_en_digits((e.raw_text or "").strip()).replace("\u200c", " ").replace("_", " ")
        if t in ("گردونه", "گردونه شانس", "گردونه‌ی شانس", "گردونه ی شانس", "گردونه روزانه", "/wheel"):
            await db.ensure_wallet(e.sender_id)
            await show_panel(e.chat_id, e.sender_id)

    @bot.on(events.CallbackQuery(pattern=rb"^w:"))
    async def on_cb(e):
        uid, act = e.sender_id, e.data.decode().split(":")[1]
        if act == "o":
            await e.answer()
            await db.ensure_wallet(uid)
            return await show_panel(e.chat_id, uid, e.message_id)
        if act == "n":
            return await e.answer(f"⏳ امروز چرخوندی. گردونه‌ی بعدی {left_text()} دیگه باز میشه.", alert=True)
        if act == "m":                                                   # «منوی اصلی» under the result card
            await e.answer()
            await botapi.delete_message(e.chat_id, e.message_id)
            return await send_menu(uid)
        if act != "s":
            return await e.answer()

        name = utils.clean_name(getattr(await e.get_sender(), "first_name", "") or "")
        await db.ensure_wallet(uid, name)
        prev = 0.0
        if uid not in admin_ids:                                         # owners can spin without limit (testing)
            prev = await db.wheel_claim(uid, day_start(), time.time())
            if prev is None:
                return await e.answer(f"⏳ امروز چرخوندی. گردونه‌ی بعدی {left_text()} دیگه باز میشه.", alert=True)
        idx, prize = draw_prize()
        try:
            bal = await db.add(uid, prize, "wheel")
        except Exception:
            log.exception("wheel payout failed")
            if uid not in admin_ids:
                await db.wheel_release(uid, prev)
            return await e.answer("⚠️ خطا؛ چرخش حساب نشد. دوباره امتحان کن.", alert=True)
        await e.answer("🎡 گردونه چرخید ...")
        chat = e.chat_id
        await botapi.delete_message(chat, e.message_id)
        try:
            async with _sem:
                gif, ms = await asyncio.to_thread(spin_gif, idx)
            m = await botapi.send_animation(chat, gif, "🎡 <b>گردونه در حال چرخشه ...</b>\n🤞 شانست رو امتحان کن!", None,
                                            int(CW * GIF_SCALE), int(CH * GIF_SCALE))
            await asyncio.sleep(ms / 1000 + 1.2)
            await botapi.delete_message(chat, m["message_id"])
        except Exception:
            log.exception("wheel animation failed (prize was already paid)")
        big = "\n🔥 <b>جایزه‌ی بزرگ!</b>" if prize >= 120 else ""
        cap = (f"🎉 <b>تبریک!</b>\n💎 <b>{prize}</b> الماس برنده شدی!{big}\n\n"
               f"💎 موجودی جدید: <b>{'نامحدود' if uid in admin_ids else db.fmt(bal)}</b>\n"
               f"⏳ گردونه‌ی بعدی: <b>{left_text()}</b> دیگه" + (" (مدیر: بدون محدودیت)" if uid in admin_ids else ""))
        markup = kb([[btn("🏠 منوی اصلی", "w:m", GREEN)]])
        try:
            async with _sem:
                png = await asyncio.to_thread(result_png, idx, prize)
            await botapi.send_photo_bytes(chat, png, cap, markup)
        except Exception:
            log.exception("wheel result card failed")
            await botapi.send_text(chat, cap, markup)
