"""🎡 Daily lucky wheel (گردونه شانس) - the wheel itself lives in the Mini App (webapp/index.html, canvas animation).

This module is only the rules:
* one free spin per Tehran calendar day (resets at 00:00 Tehran time); it is claimed atomically in the database
  (db.wheel_claim) BEFORE the prize is drawn, so double taps / two bot processes can never pay twice;
* prizes 20 40 60 80 100 120 140 200 💎 with falling odds (20 most likely, 200 rarest). Change them with the env var
      WHEEL_WEIGHTS="20:34,40:24,60:15,80:10,100:7,120:5,140:3.5,200:1.5"
* the prize is drawn on the SERVER with the OS random generator; the browser only animates to the slice it is told.
"""
import logging
import os
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from telethon import events

import botapi
import utils
from botapi import GREEN, btn, kb

log = logging.getLogger("wheel")
TZ = ZoneInfo("Asia/Tehran")
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


def seconds_left(now=None):
    """Seconds until the next 00:00 Tehran."""
    now = now or time.time()
    return max(1, int(day_start(now) + 86400 - now))


def left_text(now=None):
    """e.g. '5 ساعت و 12 دقیقه'."""
    s = seconds_left(now)
    h, m = s // 3600, s % 3600 // 60
    return f"{h} ساعت و {m} دقیقه" if h else f"{max(m, 1)} دقیقه"


# ------------------------------------------------------------------ chat shortcut: «گردونه» -> button that opens the Mini App
def register(bot, admin_ids, send_menu):
    import webapp                                    # late import: webapp imports this module

    @bot.on(events.NewMessage(func=lambda e: e.is_private and not e.out))
    async def on_text(e):
        t = utils.to_en_digits((e.raw_text or "").strip()).replace("\u200c", " ").replace("_", " ")
        if t not in ("گردونه", "گردونه شانس", "گردونه‌ی شانس", "گردونه ی شانس", "گردونه روزانه", "/wheel"):
            return
        url = webapp.public_url()
        if not url:
            return await e.respond("🎡 گردونه توی مینی‌اپ ربات هست، ولی مینی‌اپ هنوز فعال نشده؛ به مدیر خبر بده.")
        await botapi.send_text(e.chat_id, "🎡 <b>گردونه شانس روزانه</b>\nروزی یک‌بار رایگان بچرخون و تا <b>۲۰۰ 💎</b> ببر! دکمه‌ی پایین رو بزن 👇",
                               kb([[botapi.webapp_btn("🎡 باز کردن گردونه", url + "?go=wheel", GREEN)]]))
