"""The userbot ("self") that runs inside every user's Telegram account."""
import asyncio
import copy
import io
import logging
import os
import random
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp
import jdatetime
from telethon import TelegramClient, errors, events, functions, types
from telethon.sessions import StringSession

import assets
import catalog
import clockpic
from concurrent.futures import ThreadPoolExecutor
import db
import utils

log = logging.getLogger("selfbot")
TZ = ZoneInfo("Asia/Tehran")
BOT_USERNAME = ""  # set from main.py

clients: dict[int, TelegramClient] = {}
_tasks: dict[int, list] = {}

# key, label, default
TOGGLES = [
    ("afk", "😴 منشی خودکار", False),
    ("antidel", "🗑 ضد حذف/ادیت", True),
    ("ttl", "⏳ ذخیره تایمردار و ادیت‌شده‌ها", True),
    ("kw", "💬 پاسخ کلمات", True),
    ("clock", "🕒 ساعت توی اسم", False),
]

HELP = catalog.all_commands()

FREE = {"spamstop", "power", "panel", "help", "status", "bal", "give", "top", "buy"}


# ---------------- helpers ----------------
def now_fa() -> str:
    j = jdatetime.datetime.fromgregorian(datetime=datetime.now(TZ))
    return j.strftime("%Y/%m/%d  %H:%M:%S")


async def status_text(uid) -> str:
    """Exact status: diamonds, what they are worth in days / hours / minutes, and the hourly rate."""
    bal = db.fmt(await db.balance(uid))
    if uid in db.ADMINS:
        return f"**📊 وضعیت سلف**\n👑 مالک — الماس و زمان نامحدود\n🕒 {now_fa()}"
    sec = await db.remaining_seconds(uid)
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    state = "✅ فعال" if sec > 0 else "⛔ الماس کافی نیست (سلف متوقف شده)"
    paused = "" if await db.uflag(uid, "selfon", True) else "\n⏸ سلف خاموشه (ولی هزینه‌ی ساعتی همچنان کسر میشه)"
    return (f"**📊 وضعیت سلف**\n{state}{paused}\n"
            f"💎 الماس شما: {bal}\n"
            f"⏳ زمان باقی‌مانده: **{d} روز و {h} ساعت و {m} دقیقه**  (کل: {sec // 3600:,} ساعت)\n"
            f"📉 نرخ مصرف: {db.RATE_PER_HOUR:.2f} 💎 در ساعت ({db.RATE_PER_DAY:.1f} در روز)\n"
            f"🔁 {db.DIAMONDS_PER_30_DAYS:,} الماس = ۳۰ روز\n"
            f"🕒 {now_fa()}")


async def http_json(url):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.get(url) as r:
            return await r.json(content_type=None)


async def http_text(url):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.get(url) as r:
            return await r.text()


async def _reply_text(e):
    if e.is_reply:
        r = await e.get_reply_message()
        return (r.raw_text or "") if r else ""
    return ""


# ---------------- Persian argument helpers ----------------
def norm(w: str) -> str:
    """Normalise a Persian word: Arabic ي/ك -> ی/ک, drop ZWNJ and underscores."""
    return re.sub(r"[_\u200c]", "", (w or "").replace("ي", "ی").replace("ك", "ک")).lower()


def onoff(arg):
    a = norm(arg.split()[0] if arg.split() else "")
    if a in ("روشن", "on", "1", "فعال"):
        return True
    if a in ("خاموش", "off", "0", "غیرفعال"):
        return False
    return None


LANGS = {"انگلیسی": "en", "فارسی": "fa", "عربی": "ar", "ترکی": "tr", "روسی": "ru", "آلمانی": "de",
         "فرانسوی": "fr", "اسپانیایی": "es", "ایتالیایی": "it", "ژاپنی": "ja", "چینی": "zh-CN", "کره‌ای": "ko",
         "هندی": "hi", "اردو": "ur"}
LANGS = {norm(k): v for k, v in LANGS.items()}
PRICE_FA = {norm(k): v for k, v in {
    "بیت‌کوین": "btc", "بیتکوین": "btc", "اتریوم": "eth", "تون": "ton", "ترون": "trx", "تتر": "usdt",
    "بایننس": "bnb", "سولانا": "sol", "ریپل": "xrp", "دوج": "doge", "لایت‌کوین": "ltc", "یورو": "eur",
    "پوند": "gbp", "لیر": "try", "درهم": "aed", "ین": "jpy", "یوان": "cny", "روبل": "rub"}.items()}
UNITS = {"s": 1, "ث": 1, "ثانیه": 1, "m": 60, "د": 60, "دقیقه": 60, "h": 3600, "س": 3600, "ساعت": 3600,
         "d": 86400, "ر": 86400, "روز": 86400}


def parse_delay(arg):
    """'10 دقیقه متن' / '10m متن' -> (seconds, text, label) or None."""
    m = re.match(r"^(\d+)\s*(ثانیه|دقیقه|ساعت|روز|[smhdثدسر])\s+(.+)$", utils.to_en_digits(arg), re.S)
    return (int(m[1]) * UNITS[m[2]], m[3], f"{m[1]} {m[2]}") if m else None


# ---------------- commands ----------------
async def c_panel(c, e, uid, arg):
    await e.delete()
    res = await c.inline_query(BOT_USERNAME, "panel")
    if res:
        await res[0].click(e.chat_id)
    else:
        await c.send_message("me", "⚠️ پنل باز نشد؛ Inline Mode ربات مدیریت رو چک کن.")


async def c_power(c, e, uid, arg):
    v = onoff(arg)
    if v is None:
        return await e.edit("`.مدیریت_سلف روشن` یا `.مدیریت_سلف خاموش`")
    await db.uset_flag(uid, "selfon", v)
    await e.edit("✅ سلف روشن شد" if v else "⏸ سلف خاموش شد؛ ولی تا وقتی توی اکانتت هست، هزینه‌ی ساعتی کسر میشه (برای قطع کامل: «خروج و حذف سلف»)\nروشن کردن: `.مدیریت_سلف روشن`")


async def c_help(c, e, uid, arg):
    await e.edit(HELP)


async def c_status(c, e, uid, arg):
    await e.edit(await status_text(uid))


async def c_ping(c, e, uid, arg):
    t = time.time()
    await e.edit("🏓")
    await e.edit(f"🏓 Pong! `{int((time.time() - t) * 1000)}ms`")


async def c_time(c, e, uid, arg):
    await e.edit(f"🕒 {now_fa()}")


def _style(wrap):
    async def f(c, e, uid, arg):
        txt = arg or await _reply_text(e)
        if not txt:
            return await e.edit("متن رو بنویس")
        await e.edit(wrap.format(txt))
    return f


async def c_font(c, e, uid, arg):
    parts = arg.split(None, 1)
    if not parts or parts[0] not in utils.FONTS:
        lines = "\n".join(f"{k}: {f('Hello')}" for k, f in utils.FONTS.items())
        return await e.edit("🔤 فونت‌ها:\n" + lines + "\nمثال: `.فونت 1 Hello` یا ریپلای + `.فونت 2`")
    txt = parts[1] if len(parts) > 1 else await _reply_text(e)
    await e.edit(utils.FONTS[parts[0]](txt) or "—")


async def c_type(c, e, uid, arg):
    if not arg:
        return await e.edit("مثال: `.تایپ سلام`")
    out = ""
    for ch in arg:
        out += ch
        try:
            await e.edit(out + "▌")
        except Exception:
            pass
        await asyncio.sleep(0.3)
    await e.edit(out)


async def c_calc(c, e, uid, arg):
    try:
        res = utils.calc(arg)
    except Exception:
        return await e.edit("⚠️ عبارت نامعتبره. مثال: `.حساب 2*(3+4)`")
    await e.edit(f"🧮 `{arg}` = **{res}**")


# extra Google language codes accepted when typed directly (e.g. `.ترجمه ja سلام`)
LANG_CODES = set(LANGS.values()) | {
    "pt", "nl", "pl", "uk", "id", "vi", "th", "iw", "he", "sv", "el", "cs", "ro", "hu", "fi", "da", "no", "bg",
    "sr", "hr", "sk", "sl", "bn", "ta", "te", "ms", "sw", "af", "zh-tw", "la", "ca", "az", "ka", "hy", "kk", "uz"}
LANG_CODES = {x.lower() for x in LANG_CODES}
LANG_FA_NAME = {"fa": "فارسی", "en": "انگلیسی", "ar": "عربی", "tr": "ترکی", "ru": "روسی", "de": "آلمانی",
                "fr": "فرانسوی", "es": "اسپانیایی", "it": "ایتالیایی", "ja": "ژاپنی", "zh-cn": "چینی",
                "ko": "کره‌ای", "hi": "هندی", "ur": "اردو"}


def is_rtl_text(text: str) -> bool:
    """True when most letters are Persian/Arabic script."""
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    rtl = sum(1 for ch in letters if "\u0600" <= ch <= "\u06ff" or "\u0750" <= ch <= "\u077f")
    return rtl * 2 >= len(letters)


def split_lang_arg(arg: str, reply_text: str):
    """'انگلیسی سلام' | 'en سلام' | 'سلام' | '' (+reply) -> (lang_code | None, text)."""
    parts = arg.split(None, 1)
    lang, text = None, arg
    if parts:
        w = norm(parts[0])
        if w in LANGS:
            lang, text = LANGS[w], (parts[1] if len(parts) > 1 else "")
        elif w in LANG_CODES and (len(parts) > 1 or reply_text):
            lang, text = w if w != "zh-cn" else "zh-CN", (parts[1] if len(parts) > 1 else "")
    return lang, (text or reply_text or "").strip()


async def _tr_google(text, target):
    """Direct Google endpoint (no key, no library): fast and rarely blocked."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.get("https://translate.googleapis.com/translate_a/single",
                         params={"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text},
                         headers={"User-Agent": "Mozilla/5.0"}) as r:
            if r.status != 200:
                raise RuntimeError(f"google {r.status}")
            data = await r.json(content_type=None)
    out = "".join(seg[0] for seg in data[0] if seg and seg[0])
    if not out:
        raise RuntimeError("empty")
    return out, (data[2] if len(data) > 2 else "")


async def _tr_library(text, target):
    from deep_translator import GoogleTranslator
    out = await asyncio.to_thread(lambda: GoogleTranslator(source="auto", target=target).translate(text))
    if not out:
        raise RuntimeError("empty")
    return out, ""


async def _tr_mymemory(text, target):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.get("https://api.mymemory.translated.net/get",
                         params={"q": text[:480], "langpair": f"Autodetect|{target}"}) as r:
            data = await r.json(content_type=None)
    out = (data.get("responseData") or {}).get("translatedText")
    if not out or "INVALID" in out.upper() or "QUERY LENGTH" in out.upper():
        raise RuntimeError("mymemory")
    return out, ""


async def translate(text: str, target: str):
    """Tries 3 independent services; returns (translated, detected_source)."""
    last = None
    for fn in (_tr_google, _tr_library, _tr_mymemory):
        try:
            return await fn(text, target)
        except Exception as ex:                      # noqa: BLE001 - try the next service
            last = ex
            log.warning("translate via %s failed: %s", fn.__name__, ex)
    raise RuntimeError(f"all translators failed: {last}")


async def c_tr(c, e, uid, arg):
    reply = await _reply_text(e)
    lang, text = split_lang_arg(arg, reply)
    if not text:
        return await e.edit("🌐 **ترجمه**\n`.ترجمه انگلیسی سلام` یا `.ترجمه en سلام`\n"
                            "ریپلای روی متن + `.ترجمه فارسی` (یا فقط `.ترجمه` تا خودکار انتخاب کنه)")
    if len(text) > 4500:
        return await e.edit("⚠️ متن خیلی طولانیه (حداکثر ۴۵۰۰ حرف)")
    if not lang:
        lang = "en" if is_rtl_text(text) else "fa"      # Persian -> English, everything else -> Persian
    await e.edit("🌐 در حال ترجمه...")
    try:
        out, src = await translate(text, lang)
    except Exception:
        return await e.edit("⚠️ ترجمه الان ممکن نیست (سرویس‌های ترجمه جواب ندادن). چند ثانیه بعد دوباره امتحان کن.")
    tgt_name = LANG_FA_NAME.get(lang.lower(), lang)
    src_name = LANG_FA_NAME.get((src or "").lower(), src or "")
    head = f"🌐 {src_name + ' ➜ ' if src_name else ''}{tgt_name}"
    await e.edit(f"{head}\n{out}")


CRYPTO = {"btc": "bitcoin", "eth": "ethereum", "ton": "the-open-network", "trx": "tron",
          "usdt": "tether", "bnb": "binancecoin", "sol": "solana", "xrp": "ripple",
          "doge": "dogecoin", "ltc": "litecoin"}


async def c_price(c, e, uid, arg):
    s = (arg or "btc").lower().strip()
    s = PRICE_FA.get(norm(s), s)
    if s in CRYPTO:
        d = await http_json(
            f"https://api.coingecko.com/api/v3/simple/price?ids={CRYPTO[s]}&vs_currencies=usd&include_24hr_change=true")
        v = d[CRYPTO[s]]
        return await e.edit(f"💰 {s.upper()}: ${v['usd']:,}\n📈 24h: {v.get('usd_24h_change', 0):.2f}%")
    d = await http_json("https://open.er-api.com/v6/latest/USD")
    r = d.get("rates", {}).get(s.upper())
    if not r:
        return await e.edit("⚠️ ارز پیدا نشد")
    await e.edit(f"💱 1 USD = {r:,.4f} {s.upper()}")


async def c_weather(c, e, uid, arg):
    city = arg or "Tehran"
    txt = await http_text(f"https://wttr.in/{quote(city)}?format=3&lang=fa")
    await e.edit(f"🌤 {txt.strip()}")


async def c_owghat(c, e, uid, arg):
    city = arg or "Tehran"
    d = await http_json(
        f"https://api.aladhan.com/v1/timingsByCity?city={quote(city)}&country=Iran&method=7")
    t = d["data"]["timings"]
    await e.edit(
        f"🕌 **اوقات شرعی {city}**\n"
        f"اذان صبح: {t['Fajr']}\nطلوع آفتاب: {t['Sunrise']}\nاذان ظهر: {t['Dhuhr']}\n"
        f"غروب: {t['Sunset']}\nاذان مغرب: {t['Maghrib']}\nنیمه شب: {t['Midnight']}")


# Microsoft neural voices (edge-tts): Persian is supported here, unlike gTTS which has NO Persian.
VOICES = {
    "fa": ("fa-IR-DilaraNeural", "fa-IR-FaridNeural"), "en": ("en-US-JennyNeural", "en-US-GuyNeural"),
    "ar": ("ar-SA-ZariyahNeural", "ar-SA-HamedNeural"), "tr": ("tr-TR-EmelNeural", "tr-TR-AhmetNeural"),
    "ru": ("ru-RU-SvetlanaNeural", "ru-RU-DmitryNeural"), "de": ("de-DE-KatjaNeural", "de-DE-ConradNeural"),
    "fr": ("fr-FR-DeniseNeural", "fr-FR-HenriNeural"), "es": ("es-ES-ElviraNeural", "es-ES-AlvaroNeural"),
    "it": ("it-IT-ElsaNeural", "it-IT-DiegoNeural"), "ja": ("ja-JP-NanamiNeural", "ja-JP-KeitaNeural"),
    "zh-cn": ("zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural"), "ko": ("ko-KR-SunHiNeural", "ko-KR-InJoonNeural"),
    "hi": ("hi-IN-SwaraNeural", "hi-IN-MadhurNeural"), "ur": ("ur-PK-UzmaNeural", "ur-PK-AsadNeural"),
}
MALE_WORDS = {norm(x) for x in ("مرد", "آقا", "پسر", "male", "man")}
FEMALE_WORDS = {norm(x) for x in ("زن", "خانم", "دختر", "female", "woman")}


def _ffmpeg():
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


async def _tts_edge(text, lang, male):
    import edge_tts
    voice = VOICES[lang][1 if male else 0]
    buf = bytearray()

    async def run():
        async for ch in edge_tts.Communicate(text, voice).stream():
            if ch["type"] == "audio":
                buf.extend(ch["data"])

    await asyncio.wait_for(run(), 45)
    if not buf:
        raise RuntimeError("edge-tts returned no audio")
    return bytes(buf)


async def _tts_gtts(text, lang):
    from gtts import gTTS, lang as gl
    code = {"zh-cn": "zh-CN"}.get(lang, lang)
    if code not in gl.tts_langs():
        raise RuntimeError(f"gTTS does not support {code}")

    def mk():
        bio = io.BytesIO()
        gTTS(text=text, lang=code).write_to_fp(bio)
        return bio.getvalue()

    return await asyncio.to_thread(mk)


def _to_ogg_opus(mp3: bytes):
    """mp3 bytes -> ogg/opus bytes (a real Telegram voice message). None when ffmpeg is missing."""
    ff = _ffmpeg()
    if not ff:
        return None
    d = tempfile.mkdtemp()
    try:
        src, dst = os.path.join(d, "a.mp3"), os.path.join(d, "a.ogg")
        with open(src, "wb") as f:
            f.write(mp3)
        r = subprocess.run([ff, "-y", "-loglevel", "error", "-i", src, "-vn", "-c:a", "libopus", "-b:a", "40k",
                            "-ar", "48000", "-ac", "1", "-f", "ogg", dst], capture_output=True, timeout=60)
        if r.returncode != 0 or not os.path.exists(dst):
            log.warning("ffmpeg failed: %s", r.stderr[-200:])
            return None
        with open(dst, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


async def c_tts(c, e, uid, arg):
    reply = await _reply_text(e)
    words, male, lang = arg.split(), False, None
    # leading options in any order: gender word and/or language word
    while words:
        w = norm(words[0])
        if w in MALE_WORDS:
            male = True
        elif w in FEMALE_WORDS:
            male = False
        elif w in LANGS or w in LANG_CODES:
            lang = LANGS.get(w, w).lower()
        else:
            break
        words.pop(0)
    text = (" ".join(words) or reply).strip()
    if not text:
        return await e.edit("🔊 **تبدیل متن به ویس**\n`.صدا فارسی سلام دنیا`\n`.صدا مرد فارسی سلام`\n"
                            "`.صدا انگلیسی hello`\nیا ریپلای روی متن + `.صدا`")
    if len(text) > 1500:
        return await e.edit("⚠️ متن خیلی طولانیه (حداکثر ۱۵۰۰ حرف)")
    lang = lang or ("fa" if is_rtl_text(text) else "en")
    await e.edit("🔊 در حال ساخت ویس...")

    audio = None
    if lang in VOICES:
        try:
            audio = await _tts_edge(text, lang, male)
        except Exception as ex:                       # noqa: BLE001
            log.warning("edge-tts failed: %s", ex)
    if audio is None:
        try:
            audio = await _tts_gtts(text, lang)
        except Exception as ex:                       # noqa: BLE001
            log.warning("gtts failed: %s", ex)
    if audio is None:
        return await e.edit("⚠️ ساخت ویس الان ممکن نیست (سرویس صدا جواب نداد یا این زبان پشتیبانی نمیشه). دوباره امتحان کن.")

    ogg = await asyncio.to_thread(_to_ogg_opus, audio)
    reply_to = e.message.reply_to_msg_id
    chat = e.chat_id
    bio = io.BytesIO(ogg or audio)
    bio.name = "voice.ogg" if ogg else "voice.mp3"
    await e.delete()
    await c.send_file(chat, bio, reply_to=reply_to, voice_note=bool(ogg),
                      attributes=None if ogg else [types.DocumentAttributeAudio(duration=0, voice=True)])


async def c_dl(c, e, uid, arg):
    if not arg.startswith("http"):
        return await e.edit("مثال: `.دانلود لینک`")
    await e.edit("⏬ در حال دانلود...")

    def run():
        import yt_dlp
        d = tempfile.mkdtemp()
        opts = {"outtmpl": f"{d}/%(id)s.%(ext)s", "format": "best[ext=mp4]/best",
                "noplaylist": True, "quiet": True, "max_filesize": 100 * 1024 * 1024,
                "socket_timeout": 30}
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(arg, download=True)
            return y.prepare_filename(info), (info.get("title") or "")

    try:
        path, title = await asyncio.to_thread(run)
    except Exception as ex:
        return await e.edit(f"⚠️ دانلود نشد: {str(ex)[:120]}")
    try:
        await c.send_file(e.chat_id, path, caption=title[:200])
        await e.delete()
    finally:
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)


async def c_id(c, e, uid, arg):
    txt = f"💬 chat: `{e.chat_id}`"
    if e.is_reply:
        r = await e.get_reply_message()
        txt += f"\n👤 user: `{r.sender_id}`"
    await e.edit(txt)


async def c_info(c, e, uid, arg):
    if not e.is_reply:
        return await e.edit("روی پیام کاربر ریپلای کن")
    r = await e.get_reply_message()
    u = await r.get_sender()
    await e.edit(
        f"👤 **{utils.clean_name(getattr(u, 'first_name', '') or '')}** {getattr(u, 'last_name', '') or ''}\n"
        f"🆔 `{u.id}`\n🔗 @{getattr(u, 'username', None) or '—'}\n🤖 بات: {'بله' if getattr(u, 'bot', False) else 'خیر'}")


async def c_save(c, e, uid, arg):
    if not e.is_reply:
        return await e.edit("روی پیام ریپلای کن")
    r = await e.get_reply_message()
    await c.forward_messages("me", r)
    await e.edit("✅ ذخیره شد")
    await asyncio.sleep(2)
    await e.delete()


async def c_del(c, e, uid, arg):
    if arg.split()[:1] and norm(arg.split()[0]) == "ایموجی":
        return await del_emoji(c, e, uid, arg)
    n = min(int(arg), 100) if arg.isdigit() else 1
    ids = [m.id async for m in c.iter_messages(e.chat_id, from_user="me", limit=n + 1)]
    await c.delete_messages(e.chat_id, ids)


async def c_pin(c, e, uid, arg):
    if not e.is_reply:
        return await e.edit("روی پیام ریپلای کن")
    await c.pin_message(e.chat_id, e.message.reply_to_msg_id)
    await e.edit("📌 پین شد")


async def c_unpin(c, e, uid, arg):
    await c.unpin_message(e.chat_id, e.message.reply_to_msg_id if e.is_reply else None)
    await e.edit("📍 پین برداشته شد")


async def _target(e):
    """Private chat: the other person (a reply to my own message must not point at me).
    Group: the sender of the replied message."""
    if e.is_private:
        return e.chat_id
    if e.is_reply:
        try:
            r = await e.get_reply_message()
        except Exception:
            r = None
        return getattr(r, "sender_id", None)
    return None


async def _target_arg(c, e, arg):
    """Like _target, but also accepts a numeric id / @username / list index in the argument."""
    a = utils.to_en_digits((arg or "").strip())
    if a:
        if a.lstrip("-").isdigit():
            return int(a)
        try:
            return (await c.get_entity(a)).id
        except Exception:
            return None
    return await _target(e)


async def c_block(c, e, uid, arg):
    t = await _target(e)
    if not t:
        return await e.edit("ریپلای کن یا توی پیوی بزن")
    await c(functions.contacts.BlockRequest(id=await c.get_input_entity(t)))
    await e.edit("🚫 بلاک شد")


async def c_unblock(c, e, uid, arg):
    t = await _target(e)
    if not t:
        return await e.edit("ریپلای کن یا توی پیوی بزن")
    await c(functions.contacts.UnblockRequest(id=await c.get_input_entity(t)))
    await e.edit("✅ آنبلاک شد")


async def c_stats(c, e, uid, arg):
    txt = f"📊 تعداد پیام‌ها: {(await c.get_messages(e.chat_id, limit=0)).total}"
    if not e.is_private:
        try:
            txt += f"\n👥 اعضا: {(await c.get_participants(e.chat_id, limit=0)).total}"
        except Exception:
            pass
    await e.edit(txt)


async def c_afk(c, e, uid, arg):
    v = onoff(arg)
    if v is None:
        return await e.edit("`.منشی روشن` یا `.منشی خاموش`")
    await db.uset_flag(uid, "afk", v)
    await e.edit("😴 منشی خودکار روشن شد" if v else "👋 منشی خودکار خاموش شد")


async def c_afktext(c, e, uid, arg):
    if not arg:
        return await e.edit("مثال: `.متن_منشی الان نیستم، بعداً جواب میدم`")
    await db.uset(uid, "afk_text", arg)
    await e.edit("✅ متن منشی ذخیره شد")


async def c_kw(c, e, uid, arg):
    v = onoff(arg)
    if v is not None:
        await db.uset_flag(uid, "kw", v)
        return await e.edit(f"{'🟢' if v else '🔴'} پاسخ کلمه‌ای {'روشن' if v else 'خاموش'} شد")
    parts = arg.split(None, 1)
    sub = {"افزودن": "add", "اضافه": "add", "حذف": "del", "لیست": "list"}.get(norm(parts[0]), parts[0]) if parts else "list"
    rest = parts[1] if len(parts) > 1 else ""
    if sub == "add" and "|" in rest:
        k, r = [x.strip() for x in rest.split("|", 1)]
        if not k or not r:
            return await e.edit("مثال: `.کلمه افزودن سلام | سلام عزیزم`")
        await db.ar_add(uid, k, r)
        return await e.edit(f"✅ «{k}» اضافه شد")
    if sub == "del" and rest:
        await db.ar_del(uid, rest.strip())
        return await e.edit("🗑 حذف شد")
    rows = await db.ar_list(uid)
    await e.edit("💬 **پاسخ کلمات**\n" + ("\n".join(f"• {r['keyword']} ← {r['response']}" for r in rows) or "خالیه"))


# ---- live clock: name / family / bio, 12 digit fonts, updated exactly when the minute changes
DIGIT_FONTS = [
    "0123456789", "۰۱۲۳۴۵۶۷۸۹", "𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗", "𝟘𝟙𝟚𝟛𝟜𝟝𝟞𝟟𝟠𝟡", "𝟶𝟷𝟸𝟹𝟺𝟻𝟼𝟽𝟾𝟿", "𝟢𝟣𝟤𝟥𝟦𝟧𝟨𝟩𝟪𝟫",
    "𝟬𝟭𝟮𝟯𝟰𝟱𝟲𝟳𝟴𝟵", "⓪①②③④⑤⑥⑦⑧⑨", "⓿❶❷❸❹❺❻❼❽❾", "⁰¹²³⁴⁵⁶⁷⁸⁹", "₀₁₂₃₄₅₆₇₈₉", "０１２３４５６７８９",
]
CLOCK_TARGETS = {"name": ("clock", "base_name"), "last": ("clock_last", "base_last"), "bio": ("clock_bio", "base_bio")}
_CLOCK_TAIL = re.compile(r"\s*\|[^|]*:[^|]*$")


def clock_str(hm: str, font) -> str:
    if font == "rand":
        font = random.randrange(len(DIGIT_FONTS))
    table = DIGIT_FONTS[int(font) % len(DIGIT_FONTS)]
    return "".join(table[int(ch)] if ch.isdigit() else ch for ch in hm)


async def clock_font(uid):
    v = await db.uget(uid, "clock_font", "0")
    return "rand" if v == "rand" else int(v)


async def push_clock(c, uid, hm=None, restore=None):
    """ONE UpdateProfile call carrying every enabled target (restore=<target> puts the base text back)."""
    hm = hm or datetime.now(TZ).strftime("%H:%M")
    txt = clock_str(hm, await clock_font(uid))
    kw = {}
    for t, (flag, basekey) in CLOCK_TARGETS.items():
        base = await db.uget(uid, basekey, "")
        if restore == t:
            kw[{"name": "first_name", "last": "last_name", "bio": "about"}[t]] = base
        elif await db.uflag(uid, flag):
            val = f"{base} | {txt}" if base else txt
            kw[{"name": "first_name", "last": "last_name", "bio": "about"}[t]] = val[:70 if t == "bio" else 64]
    if kw:
        if "first_name" in kw and not kw["first_name"]:
            kw["first_name"] = "."
        await c(functions.account.UpdateProfileRequest(**kw))


async def set_clock_target(uid, target, on):
    """Turn the clock on/off for name | last | bio. Returns False when the self is not running."""
    c = clients.get(uid)
    if not c:
        return False
    flag, basekey = CLOCK_TARGETS[target]
    if on:
        me = await c(functions.users.GetFullUserRequest(types.InputUserSelf()))
        user = me.users[0]
        cur = {"name": user.first_name or "", "last": user.last_name or "", "bio": me.full_user.about or ""}[target]
        await db.uset(uid, basekey, _CLOCK_TAIL.sub("", cur))
        await db.uset_flag(uid, flag, True)
        await push_clock(c, uid)
    else:
        await db.uset_flag(uid, flag, False)
        await push_clock(c, uid, restore=target)
    return True


async def set_clock(uid, on):
    return await set_clock_target(uid, "name", on)


async def clock_flags(uid):
    return {t: await db.uflag(uid, f) for t, (f, _) in CLOCK_TARGETS.items()}


async def c_clock(c, e, uid, arg):
    parts = arg.split()
    tmap = {"اسم": "name", "نام": "name", "فامیل": "last", "بیو": "bio"}
    if parts and norm(parts[0]) == "فونت":
        v = parts[1] if len(parts) > 1 else ""
        if v == "رندوم" or v == "rand":
            await db.uset(uid, "clock_font", "rand")
        elif utils.to_en_digits(v).isdigit() and 1 <= int(utils.to_en_digits(v)) <= len(DIGIT_FONTS):
            await db.uset(uid, "clock_font", int(utils.to_en_digits(v)) - 1)
        else:
            return await e.edit("🔤 **فونت‌های ساعت**\n" + "\n".join(
                f"{i + 1}. {clock_str('12:34', i)}" for i in range(len(DIGIT_FONTS))) + "\nمثال: `.ساعت فونت 3` یا `.ساعت فونت رندوم`")
        await push_clock(c, uid)
        return await e.edit("🔤 فونت ساعت عوض شد " + clock_str(datetime.now(TZ).strftime("%H:%M"), await clock_font(uid)))
    target = "name"
    if parts and norm(parts[0]) in tmap:
        target, arg = tmap[norm(parts[0])], " ".join(parts[1:])
    v = onoff(arg)
    if v is None:
        fl = await clock_flags(uid)
        return await e.edit("⏰ **ساعت زنده** (دقیقاً سر هر دقیقه آپدیت میشه)\n"
                            f"اسم: {'🟢' if fl['name'] else '🔴'} | فامیل: {'🟢' if fl['last'] else '🔴'} | بیو: {'🟢' if fl['bio'] else '🔴'}\n\n"
                            "`.ساعت روشن` / `.ساعت خاموش`\n`.ساعت فامیل روشن` / `.ساعت بیو روشن`\n`.ساعت فونت 3`")
    await set_clock_target(uid, target, v)
    await e.edit(f"⏰ ساعت {'روشن' if v else 'خاموش'} شد ({ {'name': 'اسم', 'last': 'فامیل', 'bio': 'بیو'}[target] })")


# ---------------- clock on the profile PHOTO ----------------
# base photo = the user's own profile picture (stored once), a fresh JPEG with the clock is uploaded every N minutes
# and the previous clock photo is deleted, so the original pictures stay untouched underneath.
_cpic_lock: dict[int, asyncio.Lock] = {}
_cpic_last: dict[int, object] = {}          # uid -> InputPhoto of the newest clock photo (fast delete, no listing)
_POOL = ThreadPoolExecutor(max_workers=max(4, (os.cpu_count() or 2) * 2))
CPIC_LEAD = 4.0                               # start rendering/uploading this many seconds BEFORE the minute changes
_cpic_base: dict[int, bytes | None] = {}
CPIC_EVERY = (1, 2, 5, 10)


async def cpic_cfg(uid):
    return {
        "on": await db.uflag(uid, "cpic", False),
        "style": clockpic.style_key(await db.uget(uid, "cpic_style", "0")),
        "color": clockpic.color_key(await db.uget(uid, "cpic_color", "red")),
        "every": int(await db.uget(uid, "cpic_every", "1") or 1),
    }


async def cpic_get_base(c, uid, capture=False):
    """Stored base photo bytes. capture=True: if none stored, take the current profile photo (once)."""
    import base64
    if uid not in _cpic_base:
        v = await db.uget(uid, "cpic_base")
        _cpic_base[uid] = base64.b64decode(v) if v else None
    if _cpic_base[uid] is None and capture:
        try:
            raw = await c.download_profile_photo("me", file=bytes)
            if raw:
                await cpic_set_base(uid, raw)
        except Exception as ex:                                  # noqa: BLE001
            log.warning("profile photo capture failed: %s", ex)
    return _cpic_base.get(uid)


async def cpic_set_base(uid, raw: bytes):
    import base64
    norm_raw = await asyncio.to_thread(clockpic.normalize_base, raw)
    _cpic_base[uid] = norm_raw
    await db.uset(uid, "cpic_base", base64.b64encode(norm_raw).decode())


async def cpic_clear_base(uid):
    _cpic_base[uid] = None
    await db.udel(uid, "cpic_base")


async def cpic_render(c, uid, now=None) -> bytes:
    cfg = await cpic_cfg(uid)
    base = await cpic_get_base(c, uid)
    return await asyncio.get_running_loop().run_in_executor(
        _POOL, clockpic.render, base, cfg["style"], cfg["color"], now or datetime.now(TZ))


async def _cpic_delete(c, ids, keep=None):
    if not ids:
        return
    photos = await c.get_profile_photos("me", limit=12)
    dead = [types.InputPhoto(id=p.id, access_hash=p.access_hash, file_reference=p.file_reference)
            for p in photos if p.id in ids and p.id != keep]
    if dead:
        await c(functions.photos.DeletePhotosRequest(id=dead))


async def _cpic_cleanup(c, uid, new_photo, prev_input):
    """Delete the previous clock photo in the background (never blocks the next update)."""
    new_id = new_photo.id
    try:
        old = {int(x) for x in (await db.uget(uid, "cpic_ids", "")).split(",") if x}
        old.discard(new_id)
        if prev_input is not None:
            try:
                await c(functions.photos.DeletePhotosRequest(id=[prev_input]))
                old.discard(prev_input.id)
            except Exception:                                    # noqa: BLE001
                pass
        if old:
            await _cpic_delete(c, old, keep=new_id)
        await db.uset(uid, "cpic_ids", str(new_id))
    except errors.FloodWaitError:
        pass
    except Exception as ex:                                      # noqa: BLE001
        log.warning("old clock photo not deleted: %s", ex)


async def push_clockpic(c, uid, when=None):
    """Render + upload the clock photo; the previous one is removed in the background.
    when: the (future) time to draw. The file is uploaded first and the profile photo is switched exactly
    when that minute starts, so viewers see the new time with no delay."""
    async with _cpic_lock.setdefault(uid, asyncio.Lock()):
        t = when or datetime.now(TZ)
        jpg = await cpic_render(c, uid, t)
        bio = io.BytesIO(jpg)
        bio.name = "clock.jpg"
        up = await c.upload_file(bio)
        if when is not None:
            wait = (when.replace(second=0, microsecond=0) - datetime.now(TZ)).total_seconds()
            if 0 < wait < 15:
                await asyncio.sleep(wait)
        res = await c(functions.photos.UploadProfilePhotoRequest(file=up))
        ph = res.photo
        prev = _cpic_last.get(uid)
        _cpic_last[uid] = types.InputPhoto(id=ph.id, access_hash=ph.access_hash, file_reference=ph.file_reference)
        old = (await db.uget(uid, "cpic_ids", "")).split(",")
        await db.uset(uid, "cpic_ids", ",".join([x for x in old if x] + [str(ph.id)]))
        asyncio.create_task(_cpic_cleanup(c, uid, ph, prev))


async def set_clockpic(uid, on) -> str:
    """Turn the photo clock on/off. Returns '' on success or an error text."""
    c = clients.get(uid)
    if not c:
        return "سلف روشن نیست"
    if on:
        base = await cpic_get_base(c, uid, capture=True)
        await db.uset_flag(uid, "cpic", True)
        try:
            await push_clockpic(c, uid)
        except errors.FloodWaitError as ex:
            return f"تلگرام برای تغییر عکس پروفایل {ex.seconds} ثانیه صبر خواست؛ بعداً خودکار ادامه میده"
        except Exception as ex:                                  # noqa: BLE001
            log.warning("clockpic on failed: %s", ex)
            await db.uset_flag(uid, "cpic", False)
            return f"آپلود عکس ناموفق بود ({type(ex).__name__})"
        return "" if base else ""
    await db.uset_flag(uid, "cpic", False)
    try:
        ids = {int(x) for x in (await db.uget(uid, "cpic_ids", "")).split(",") if x}
        await _cpic_delete(c, ids)
        await db.udel(uid, "cpic_ids")
    except Exception as ex:                                      # noqa: BLE001
        log.warning("clock photo removal failed: %s", ex)
        return "عکس ساعت پاک نشد؛ دستی از پروفایل حذفش کن"
    return ""


async def cpic_refresh(uid):
    """After a style / color / interval change: show it right away (if the photo clock is on)."""
    c = clients.get(uid)
    if c and await db.uflag(uid, "cpic", False):
        try:
            await push_clockpic(c, uid)
        except Exception as ex:                                  # noqa: BLE001
            log.warning("clockpic refresh: %s", ex)


def _pick_color(w):
    k = norm(w)
    for key, (fa, _e, _c) in clockpic.COLORS.items():
        if k in (key, norm(fa)):
            return key
    if utils.to_en_digits(w).isdigit() and 1 <= int(utils.to_en_digits(w)) <= len(clockpic.COLOR_ORDER):
        return clockpic.COLOR_ORDER[int(utils.to_en_digits(w)) - 1]
    return None


def _pick_style(w):
    """Style by key, full/partial Persian name (emoji and spaces ignored) or its number: `.ساعت_عکس طرح شیر` / `طرح 18`."""
    canon = lambda t: re.sub(r"[\W_]+", "", utils.to_en_digits(str(t)).lower())
    k = canon(w)
    if not k:
        return None
    for key, fa in clockpic.STYLES:
        if k in (canon(key), canon(fa)):
            return key
    if k.isdigit() and 1 <= int(k) <= len(clockpic.STYLES):
        return clockpic.STYLES[int(k) - 1][0]
    for key, fa in clockpic.STYLES:                      # partial name, e.g. "شیر" -> first lion & sun design
        if len(k) >= 2 and k in canon(fa):
            return key
    return None


def cpic_help(cfg):
    st = dict(clockpic.STYLES)[cfg["style"]]
    return (f"🖼 **ساعت روی عکس پروفایل** {'🟢 روشن' if cfg['on'] else '🔴 خاموش'}\n"
            f"🎨 طرح: {st} | رنگ: {clockpic.COLORS[cfg['color']][0]} | هر {cfg['every']} دقیقه\n\n"
            "`.ساعت_عکس روشن` / `.ساعت_عکس خاموش`\n"
            "`.ساعت_عکس رنگ آبی` (قرمز، آبی، سبز، بنفش، طلایی، صورتی، فیروزه‌ای، نارنجی، سفید، دارک یا شماره ۱ تا ۱۰)\n"
            "`.ساعت_عکس طرح 3` (" + "، ".join(f"{i}={n}" for i, (_k, n) in enumerate(clockpic.STYLES, 1)) + ")\n"
            "`.ساعت_عکس فاصله 2` (هر ۱ / ۲ / ۵ / ۱۰ دقیقه)\n"
            "`.ساعت_عکس پیش‌نمایش` — نمونه توی Saved Messages\n"
            "ریپلای روی یه عکس + `.ساعت_عکس عکس` — عکس پایه‌ی دلخواه\n"
            "`.ساعت_عکس بازنشانی` — خاموش + فراموش کردن عکس پایه")


async def c_clockpic(c, e, uid, arg):
    w = arg.split()
    sub = norm(w[0]) if w else ""
    cfg = await cpic_cfg(uid)
    if not w:
        return await e.edit(cpic_help(cfg))
    if sub in ("روشن", "on", "فعال", "خاموش", "off", "غیرفعال"):
        on = sub in ("روشن", "on", "فعال")
        await e.edit("⏳ در حال " + ("ساخت ساعت روی عکس ..." if on else "برداشتن ساعت از عکس ..."))
        err = await set_clockpic(uid, on)
        return await e.edit(f"{'🟢 ساعت روی عکس روشن شد' if on else '🔴 ساعت روی عکس خاموش شد (عکس قبلیت برگشت)'}" + (f"\n⚠️ {err}" if err else ""))
    if sub in ("رنگ", "color"):
        k = _pick_color(w[1]) if len(w) > 1 else None
        if not k:
            return await e.edit("🎨 رنگ‌ها: " + " | ".join(f"{i}. {v[1]} {v[0]}" for i, v in enumerate(clockpic.COLORS.values(), 1))
                                + "\nمثال: `.ساعت_عکس رنگ بنفش`")
        await db.uset(uid, "cpic_color", k)
        await e.edit(f"🎨 رنگ ساعت: {clockpic.COLORS[k][1]} {clockpic.COLORS[k][0]}")
        return await cpic_refresh(uid)
    if sub in ("طرح", "style", "مدل"):
        k = _pick_style(" ".join(w[1:])) if len(w) > 1 else None
        if not k:
            return await e.edit("🖼 طرح‌ها: " + " | ".join(f"{i}. {n}" for i, (_k, n) in enumerate(clockpic.STYLES, 1))
                                + "\nمثال: `.ساعت_عکس طرح 4` | `.ساعت_عکس طرح شیر` | `.ساعت_عکس طرح 19`")
        await db.uset(uid, "cpic_style", k)
        await e.edit(f"🖼 طرح ساعت: {dict(clockpic.STYLES)[k]}")
        return await cpic_refresh(uid)
    if sub in ("فاصله", "هر", "interval"):
        n = int(utils.to_en_digits(w[1])) if len(w) > 1 and utils.to_en_digits(w[1]).isdigit() else 0
        if n not in CPIC_EVERY:
            return await e.edit("فاصله‌ی آپدیت: `.ساعت_عکس فاصله 1` (۱ / ۲ / ۵ / ۱۰ دقیقه)")
        await db.uset(uid, "cpic_every", n)
        return await e.edit(f"⏱ عکس ساعت هر {n} دقیقه آپدیت میشه")
    if sub in ("پیشنمایش", "نمونه", "preview"):
        await cpic_get_base(c, uid, capture=not cfg["on"])
        bio = io.BytesIO(await cpic_render(c, uid))
        bio.name = "clock_preview.jpg"
        await c.send_file("me", bio, caption="👁 پیش‌نمایش ساعت روی عکس")
        return await e.edit("👁 پیش‌نمایش توی Saved Messages فرستاده شد")
    if sub in ("عکس", "photo"):
        r = await e.get_reply_message() if e.is_reply else None
        if not r or not (r.photo or (r.document and (r.document.mime_type or "").startswith("image/"))):
            return await e.edit("روی یه **عکس** ریپلای کن و بزن `.ساعت_عکس عکس`")
        await cpic_set_base(uid, await r.download_media(file=bytes))
        await e.edit("✅ عکس پایه ذخیره شد")
        return await cpic_refresh(uid)
    if sub in ("بازنشانی", "ریست", "reset"):
        err = await set_clockpic(uid, False)
        await cpic_clear_base(uid)
        return await e.edit("♻️ ساعت روی عکس خاموش شد و عکس پایه پاک شد؛ دفعه‌ی بعد از عکس پروفایل فعلی ساخته میشه" + (f"\n⚠️ {err}" if err else ""))
    await e.edit(cpic_help(cfg))


async def clockpic_loop(c, uid):
    """Wake up CPIC_LEAD seconds before every minute, pre-render the NEXT minute, upload it and switch the
    profile photo exactly at the minute boundary."""
    while True:
        try:
            now = datetime.now(TZ)
            wait = 60 - now.second - now.microsecond / 1e6 - CPIC_LEAD
            if wait <= 0:
                wait += 60
            await asyncio.sleep(wait)
            nxt = (datetime.now(TZ) + timedelta(seconds=CPIC_LEAD + 1)).replace(second=0, microsecond=0)
            if not await db.uflag(uid, "cpic", False):
                continue
            if not await db.user_is_active(uid) or not await db.uflag(uid, "selfon", True):
                continue
            every = int(await db.uget(uid, "cpic_every", "1") or 1)
            if nxt.minute % every:
                continue
            await push_clockpic(c, uid, when=nxt)
        except asyncio.CancelledError:
            raise
        except errors.FloodWaitError as ex:
            await asyncio.sleep(min(ex.seconds, 900) + 1)
        except Exception as ex:                                  # noqa: BLE001
            log.warning("clockpic %s: %s", uid, ex)
            await asyncio.sleep(3)


async def c_remind(c, e, uid, arg):
    d = parse_delay(arg)
    if not d:
        return await e.edit("مثال: `.یادآور 10 دقیقه تماس بگیر`")
    secs, text, label = d
    if secs > 86400:
        return await e.edit("حداکثر ۲۴ ساعت")
    await e.edit(f"⏰ یادآوری تنظیم شد ({label})")

    async def later():
        await asyncio.sleep(secs)
        await c.send_message("me", f"⏰ یادآوری: {text}")

    asyncio.create_task(later())


async def c_pass(c, e, uid, arg):
    n = min(int(arg), 64) if arg.isdigit() else 16
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%&*"
    await e.edit("🔑 `" + "".join(secrets.choice(alphabet) for _ in range(n)) + "`")


async def c_rand(c, e, uid, arg):
    nums = re.findall(r"\d+", utils.to_en_digits(arg))
    a, b = (int(nums[0]), int(nums[1])) if len(nums) >= 2 else (1, 100)
    await e.edit(f"🎲 {random.randint(min(a, b), max(a, b))}")


async def c_choose(c, e, uid, arg):
    opts = [x.strip() for x in arg.split("|") if x.strip()]
    if len(opts) < 2:
        return await e.edit("مثال: `.انتخاب الف|ب|ج`")
    await e.edit(f"🎯 {random.choice(opts)}")


def _dice(emoji):
    async def f(c, e, uid, arg):
        await e.delete()
        await c.send_message(e.chat_id, file=types.InputMediaDice(emoji))
    return f


# ---------------- diamonds ----------------
async def c_bal(c, e, uid, arg):
    bal = await db.balance(uid)
    text = f"💎 موجودی شما: **{db.fmt(bal)}** الماس\n💰 ارزش: **{db.value_text(bal)}**"
    if assets.BANNER_PATH:                      # show the banner image here too
        try:
            reply_to = e.reply_to_msg_id
            await e.delete()
            try:
                return await c.send_file(e.chat_id, assets.BANNER_PATH, caption=text, reply_to=reply_to)
            except Exception:
                return await c.send_message(e.chat_id, text, reply_to=reply_to)
        except Exception:
            pass
    await e.edit(text)


async def c_top(c, e, uid, arg):
    rows = await db.top(10)
    lines = [f"{i}. {utils.clean_name(r['name'] or str(r['user_id']))} — {r['diamonds']:,} 💎"
             for i, r in enumerate(rows, 1)]
    await e.edit("🏆 **برترین‌ها**\n" + ("\n".join(lines) or "هنوز کسی نیست"))


async def c_buy(c, e, uid, arg):
    await e.edit(f"💳 **خرید الماس**\n💎 ۱۵۰۰ الماس = **{db.price_toman(1500):,}** تومان\n"
                 f"از ربات مدیریت (@{BOT_USERNAME}) دکمه‌ی «💳 خرید الماس» رو بزن.")


async def c_give(c, e, uid, arg):
    m = re.search(r"\d+", utils.to_en_digits(arg))
    t = await _target(e)
    if not m or int(m[0]) <= 0 or not t:
        return await e.edit("مثال: ریپلای کن + `.انتقال 25` (یا توی پیوی `.انتقال 25`)")
    if t == uid:
        return await e.edit("به خودت نمیشه 😉")
    res = await db.transfer(uid, t, int(m[0]))
    if res is None:
        return await e.edit(f"💎 الماس کافی نیست (موجودی: {db.fmt(await db.balance(uid))})")
    await e.edit(f"✅ **{int(m[0]):,}** 💎 منتقل شد\nموجودی تو: {db.fmt(res[0])}")


# ---------------- special features ----------------
async def c_later(c, e, uid, arg):
    d = parse_delay(arg)
    if not d:
        return await e.edit("مثال: `.زمانبندی 30 دقیقه سلام، یادت نره!`")
    secs, text, label = d
    secs = max(secs, 20)
    if secs > 30 * 86400:
        return await e.edit("حداکثر ۳۰ روز")
    await c.send_message(e.chat_id, text, schedule=timedelta(seconds=secs))
    await e.edit(f"🗓 پیام زمان‌بندی شد ({label} دیگه ارسال میشه)")
    await asyncio.sleep(3)
    await e.delete()


async def c_cd(c, e, uid, arg):
    n = int(utils.to_en_digits(arg)) if utils.to_en_digits(arg).isdigit() else 5
    n = max(1, min(n, 30))
    for i in range(n, 0, -1):
        try:
            await e.edit(f"⏳ **{i}**")
        except Exception:
            pass
        await asyncio.sleep(1)
    await e.edit("🚀 **تمام شد!**")


async def c_qr(c, e, uid, arg):
    txt = arg or await _reply_text(e)
    if not txt:
        return await e.edit("مثال: `.کیوآر https://t.me/...`")

    def mk():
        import qrcode
        bio = io.BytesIO()
        qrcode.make(txt).save(bio, "PNG")
        bio.seek(0)
        bio.name = "qr.png"
        return bio

    bio = await asyncio.to_thread(mk)
    reply_to = e.message.reply_to_msg_id
    await e.delete()
    await c.send_file(e.chat_id, bio, reply_to=reply_to)


async def c_typing(c, e, uid, arg):
    n = int(utils.to_en_digits(arg)) if utils.to_en_digits(arg).isdigit() else 5
    chat = e.chat_id
    await e.delete()
    async with c.action(chat, "typing"):
        await asyncio.sleep(max(1, min(n, 60)))


async def c_purge(c, e, uid, arg):
    if not e.is_reply:
        return await e.edit("روی اولین پیامی که می‌خوای از اونجا پاک بشه ریپلای کن")
    start = e.message.reply_to_msg_id
    ids = [m.id async for m in c.iter_messages(e.chat_id, from_user="me", min_id=start - 1, limit=200)]
    ids.append(e.id)
    await c.delete_messages(e.chat_id, list(set(ids)))


async def c_bio(c, e, uid, arg):
    if not arg:
        return await e.edit("مثال: `.بیو سلام من فلانی‌ام`")
    await c(functions.account.UpdateProfileRequest(about=arg[:70]))
    await e.edit("✅ بیو تغییر کرد")


async def c_setname(c, e, uid, arg):
    if not arg:
        return await e.edit("مثال: `.اسم علی`")
    await db.uset(uid, "base_name", arg[:50])
    await c(functions.account.UpdateProfileRequest(first_name=arg[:64]))
    await e.edit("✅ اسم تغییر کرد")


WEEKDAYS = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]
MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
          "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]


def date_text() -> str:
    g = datetime.now(TZ)
    j = jdatetime.datetime.fromgregorian(datetime=g)
    return (f"📅 **{WEEKDAYS[j.weekday()]} {j.day} {MONTHS[j.month - 1]} {j.year}**\n"
            f"🌍 {g.strftime('%Y-%m-%d')}\n🕒 {g.strftime('%H:%M:%S')}")


async def c_date(c, e, uid, arg):
    await e.edit(date_text())


async def c_hack(c, e, uid, arg):
    frames = ["💻 در حال اتصال به سرور...", "🔎 اسکن پورت‌ها [▓▓░░░░░░░░] 20%",
              "🔓 دور زدن فایروال [▓▓▓▓▓░░░░░] 50%", "🧬 استخراج اطلاعات [▓▓▓▓▓▓▓▓░░] 80%",
              "📡 ارسال داده [▓▓▓▓▓▓▓▓▓▓] 100%", "✅ **هک شد!** ... شوخی بود 😄"]
    for f in frames:
        await e.edit(f)
        await asyncio.sleep(1)


async def c_love(c, e, uid, arg):
    for h in ["❤️", "🧡", "💛", "💚", "💙", "💜", "🤍", "💖"] * 2:
        await e.edit(h * 5)
        await asyncio.sleep(0.5)
    await e.edit("❤️ **دوستت دارم** ❤️")


EIGHT = ["حتماً ✅", "بدون شک 💯", "آره 👍", "به‌احتمال زیاد", "نمی‌دونم 🤔", "دوباره بپرس",
         "فکر نکنم", "نه ❌", "اصلاً 🚫", "شانسش کمه"]


async def c_8ball(c, e, uid, arg):
    await e.edit(f"🎱 {arg or ''}\n**{random.choice(EIGHT)}**")


async def c_note(c, e, uid, arg):
    parts = arg.split(None, 1)
    sub = parts[0] if parts else "لیست"
    rest = parts[1].strip() if len(parts) > 1 else ""
    nsub = norm(sub)
    if nsub in ("ذخیره", "افزودن") and "|" in rest:
        k, v = [x.strip() for x in rest.split("|", 1)]
        if not k or not v:
            return await e.edit("مثال: `.یادداشت ذخیره شماره | 09120000000`")
        await db.uset(uid, "note:" + k.lower(), v)
        return await e.edit(f"📝 یادداشت «{k}» ذخیره شد")
    if nsub == "حذف" and rest:
        await db.udel(uid, "note:" + rest.lower())
        return await e.edit("🗑 حذف شد")
    if nsub == "لیست":
        rows = await db.uprefix(uid, "note:")
        return await e.edit("📝 **یادداشت‌ها**\n" + ("\n".join(f"• {r['key'][5:]}" for r in rows) or "خالیه"))
    v = await db.uget(uid, "note:" + arg.strip().lower())
    await e.edit(f"📝 {v}" if v else "پیدا نشد. `.یادداشت لیست`")


async def c_coin(c, e, uid, arg):
    await e.edit("🪙 **" + random.choice(["شیر", "خط"]) + "**")


async def c_reverse(c, e, uid, arg):
    txt = arg or await _reply_text(e)
    await e.edit(txt[::-1] if txt else "مثال: `.معکوس سلام`")


async def c_me(c, e, uid, arg):
    me = await c.get_me()
    await e.edit(f"👤 **{utils.clean_name(me.first_name or '')}** {me.last_name or ''}\n🆔 `{me.id}`\n"
                 f"🔗 @{me.username or '—'}\n📱 `+{me.phone or '—'}`\n⭐ پریمیوم: {'بله' if getattr(me, 'premium', False) else 'خیر'}")


async def c_copy(c, e, uid, arg):
    if not e.is_reply:
        return await e.edit("روی پیام ریپلای کن")
    r = await e.get_reply_message()
    await e.delete()
    await c.send_message(e.chat_id, r, reply_to=r.reply_to_msg_id if r.is_reply else None)


async def c_selfdestruct(c, e, uid, arg):
    parts = utils.to_en_digits(arg).split(None, 1)
    if len(parts) < 2 or not parts[0].isdigit():
        return await e.edit("مثال: `.تخریب 10 این پیام ده ثانیه دیگه پاک میشه`")
    n = max(2, min(int(parts[0]), 3600))
    await e.edit(parts[1])

    async def kill():
        await asyncio.sleep(n)
        try:
            await e.delete()
        except Exception:
            pass

    asyncio.create_task(kill())


async def c_invite(c, e, uid, arg):
    await e.edit(f"👥 لینک دعوت تو (هر نفر = {db.REF_BONUS} 💎):\n`https://t.me/{BOT_USERNAME}?start=ref{uid}`")


# ---------------- spam / premium emoji / privacy / group tools ----------------
def u16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


_spam_stop: set[int] = set()


async def c_spamstop(c, e, uid, arg):
    _spam_stop.add(uid)
    await e.edit("🛑 اسپم متوقف شد")


async def c_spam(c, e, uid, arg):
    m = re.match(r"^(\d+)\s+(.+)$", utils.to_en_digits(arg), re.S)
    if not m:
        return await e.edit("مثال: `.اسپم 10 سلام` (حداکثر ۵۰ پیام، با فاصله‌ی امن)")
    n, text = min(int(m[1]), 50), m[2]
    chat, reply_to = e.chat_id, e.message.reply_to_msg_id
    await e.delete()
    _spam_stop.discard(uid)
    for _ in range(n):
        if uid in _spam_stop:
            _spam_stop.discard(uid)
            break
        try:
            await c.send_message(chat, text, reply_to=reply_to)
        except errors.FloodWaitError as ex:
            await asyncio.sleep(ex.seconds + 1)
            continue
        except Exception:
            break
        await asyncio.sleep(random.uniform(0.9, 1.4))


EMOJI_PREFIX = "emoji:"
_emoji_cache: dict[int, dict] = {}


async def emoji_map(uid):
    if uid not in _emoji_cache:
        rows = await db.uprefix(uid, EMOJI_PREFIX)
        _emoji_cache[uid] = {r["key"][len(EMOJI_PREFIX):]: int(r["value"]) for r in rows}
    return _emoji_cache[uid]


def custom_ids(msg):
    return [en for en in (msg.entities or []) if isinstance(en, types.MessageEntityCustomEmoji)]


async def save_emoji(uid, free, doc_id):
    await db.uset(uid, EMOJI_PREFIX + free, doc_id)
    _emoji_cache.pop(uid, None)


async def c_register(c, e, uid, arg):
    """.ثبت ایموجی <ایموجی پریمیوم> [ایموجی رایگان]"""
    parts = arg.split(None, 1)
    if not parts or norm(parts[0]) != "ایموجی":
        return await e.edit("مثال: `.ثبت ایموجی ` + یک ایموجی پریمیوم")
    ents = custom_ids(e.message)
    if not ents:
        return await e.edit("⚠️ توی پیامت ایموجی پریمیوم نبود. دستور رو همراه یه ایموجی پریمیوم بفرست "
                            "(اکانتت باید پریمیوم باشه).\nمثال: `.ثبت ایموجی ` + 😀پریمیوم")
    ent = ents[0]
    raw = e.raw_text
    b = raw.encode("utf-16-le")
    rest = (b[:ent.offset * 2] + b[(ent.offset + ent.length) * 2:]).decode("utf-16-le")
    rest = re.sub(r"^\.\S+\s+ایموجی", "", rest).strip()
    if rest and len(rest) <= 12:
        await save_emoji(uid, rest, ent.document_id)
        return await e.edit(f"✅ ثبت شد: {rest} ➜ ایموجی پریمیوم\nهرجا {rest} بفرستی خودکار تبدیل میشه.")
    reg_state[uid] = (ent.document_id, time.time())
    await e.edit("✅ ایموجی پریمیوم ثبت شد.\n👉 حالا **ایموجی رایگانی** که می‌خوای به این تبدیل بشه رو "
                 "همین‌جا بفرست (فقط خود ایموجی، تا ۲ دقیقه).")


reg_state: dict[int, tuple] = {}

# ---- premium emoji for NON-premium accounts: Telegram strips custom emoji from messages a free account sends,
# so the text is sent through the manager bot's inline mode instead (the bot is allowed to carry custom emoji).
INLINE_PAYLOADS: dict[str, dict] = {}
_premium: dict[int, tuple] = {}          # uid -> (checked_at, is_premium)
_inline_warned: set = set()

_BOTAPI_ENT = {
    types.MessageEntityBold: "bold", types.MessageEntityItalic: "italic", types.MessageEntityCode: "code",
    types.MessageEntityUnderline: "underline", types.MessageEntityStrike: "strikethrough",
    types.MessageEntitySpoiler: "spoiler", types.MessageEntityBlockquote: "blockquote",
}


def to_botapi_entities(ents):
    """Telethon entities -> Bot API entity dicts (formatting + custom emoji; others are dropped)."""
    out = []
    for en in ents:
        base = {"offset": en.offset, "length": en.length}
        if isinstance(en, types.MessageEntityCustomEmoji):
            out.append({**base, "type": "custom_emoji", "custom_emoji_id": str(en.document_id)})
        elif isinstance(en, types.MessageEntityTextUrl):
            out.append({**base, "type": "text_link", "url": en.url})
        elif isinstance(en, types.MessageEntityPre):
            out.append({**base, "type": "pre", **({"language": en.language} if en.language else {})})
        else:
            t = _BOTAPI_ENT.get(type(en))
            if t:
                out.append({**base, "type": t})
    return out


async def is_premium(c, uid) -> bool:
    hit = _premium.get(uid)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        ok = bool(getattr(await c.get_me(), "premium", False))
    except Exception:
        ok = False
    _premium[uid] = (time.time(), ok)
    return ok


async def send_via_bot(c, e, uid, text, ents) -> bool:
    """Send `text` (+ premium emoji entities) through the manager bot's inline mode and delete the original."""
    if not BOT_USERNAME or e.message.media:
        return False
    now = time.time()
    for k in [k for k, v in INLINE_PAYLOADS.items() if now - v["ts"] > 120]:
        INLINE_PAYLOADS.pop(k, None)
    token = secrets.token_hex(6)
    INLINE_PAYLOADS[token] = {"uid": uid, "text": text, "entities": to_botapi_entities(ents), "ts": now}
    try:
        res = await c.inline_query(BOT_USERNAME, f"emo:{token}")
        if not res:
            return False
        await res[0].click(e.chat_id, reply_to=e.message.reply_to_msg_id)
    except Exception as ex:                          # noqa: BLE001
        log.warning("inline premium-emoji send failed: %s", ex)
        return False
    finally:
        INLINE_PAYLOADS.pop(token, None)
    try:
        await e.delete()
    except Exception:
        pass
    return True



async def c_list_cmd(c, e, uid, arg):
    if norm(arg.split()[0] if arg.split() else "") != "ایموجی":
        return await e.edit("مثال: `.لیست ایموجی`")
    m = await emoji_map(uid)
    await e.edit("😀 **ایموجی‌های ثبت‌شده**\n" + ("\n".join(f"{k} ➜ `{v}`" for k, v in m.items()) or "خالیه"))


async def del_emoji(c, e, uid, arg):
    free = arg.split(None, 1)[1].strip() if len(arg.split(None, 1)) > 1 else ""
    if not free:
        await db.pool.execute("DELETE FROM user_settings WHERE user_id=$1 AND key LIKE $2", uid, EMOJI_PREFIX + "%")
        for k in [k for k in db._ucache if k[0] == uid and k[1].startswith(EMOJI_PREFIX)]:
            db._ucache.pop(k, None)
        _emoji_cache.pop(uid, None)
        return await e.edit("🗑 همه‌ی ایموجی‌های ثبت‌شده حذف شد")
    await db.udel(uid, EMOJI_PREFIX + free)
    _emoji_cache.pop(uid, None)
    await e.edit(f"🗑 {free} حذف شد")


# ---------------- private backup group (timed media + deleted/edited messages) ----------------
BACKUP_TITLE = "𝚾𝚻𝚳 𝙎𝙀𝙇𝙁 backup"
_bk_peer: dict[int, object] = {}
_bk_lock: dict[int, asyncio.Lock] = {}
_bk_warned: set = set()
_BK_GONE = tuple(getattr(errors, n) for n in (
    "ChannelPrivateError", "ChannelInvalidError", "ChatWriteForbiddenError", "PeerIdInvalidError",
    "ChatIdInvalidError", "UserBannedInChannelError", "ChannelBannedError") if hasattr(errors, n))


def _bk_parse(v):
    try:
        cid, h = str(v).split(":")
        return types.InputPeerChannel(int(cid), int(h))
    except Exception:
        return None


async def backup_peer(c, uid, create=True, fresh=False):
    """The user's private backup group (created once, remembered in the DB). None if it cannot be made."""
    if fresh:
        _bk_peer.pop(uid, None)
        await db.udel(uid, "backup_chat")
    if uid in _bk_peer:
        return _bk_peer[uid]
    async with _bk_lock.setdefault(uid, asyncio.Lock()):
        if uid in _bk_peer:
            return _bk_peer[uid]
        peer = _bk_parse(await db.uget(uid, "backup_chat"))
        if peer is None and create:
            res = await c(functions.channels.CreateChannelRequest(
                title=BACKUP_TITLE, megagroup=True,
                about="بکاپ خصوصی سلف: عکس/ویدیوی تایمردار و پیام‌های حذف یا ادیت‌شده اینجا ذخیره میشن."))
            ch = res.chats[0]
            peer = types.InputPeerChannel(ch.id, ch.access_hash)
            await db.uset(uid, "backup_chat", f"{ch.id}:{ch.access_hash}")
            try:                                   # no notification spam from the backup group
                await c(functions.account.UpdateNotifySettingsRequest(
                    peer=types.InputNotifyPeer(peer=peer),
                    settings=types.InputPeerNotifySettings(mute_until=2 ** 31 - 1)))
            except Exception:                      # noqa: BLE001
                pass
            try:
                await c.send_message(peer, "📦 **بکاپ خصوصی سلف**\nعکس/ویدیوی تایمردار و پیام‌های حذف یا ادیت‌شده‌ی پیوی از الان اینجا ذخیره میشن.\n"
                                           "⚠️ این گپ رو حذف نکن؛ اگه حذفش کنی خودکار یکی جدید ساخته میشه.")
            except Exception:                      # noqa: BLE001
                pass
        if peer is not None:
            _bk_peer[uid] = peer
        return peer


async def ensure_backup(uid) -> bool:
    """Called when the user switches timed-media / anti-delete on: make the group right away."""
    c = clients.get(uid)
    if not c:
        return False
    try:
        return await backup_peer(c, uid) is not None
    except Exception as ex:                        # noqa: BLE001
        log.warning("backup group for %s not created: %s", uid, ex)
        return False


async def backup_send(c, uid, text=None, file=None, caption=None):
    """Send into the backup group (re-created if the user deleted it). Saved Messages is only the last resort."""
    gone = False
    for _ in range(3):
        try:
            peer = await backup_peer(c, uid, fresh=gone)
            gone = False
            if peer is None:
                break
            if file is not None:
                return await c.send_file(peer, file, caption=caption)
            return await c.send_message(peer, text)
        except errors.FloodWaitError as ex:
            if ex.seconds > 20:
                break
            await asyncio.sleep(ex.seconds + 1)
        except _BK_GONE:
            gone = True
        except Exception as ex:                    # noqa: BLE001
            log.warning("backup send failed for %s: %s", uid, ex)
            break
    if uid not in _bk_warned:
        _bk_warned.add(uid)
        try:
            await c.send_message("me", f"⚠️ ساخت یا استفاده از گپ «{BACKUP_TITLE}» ممکن نشد "
                                       "(شاید به سقف تعداد گروه‌های تلگرام رسیدی). تا درست شدن، بکاپ‌ها اینجا میان.")
        except Exception:                          # noqa: BLE001
            pass
    if file is not None:
        return await c.send_file("me", file, caption=caption)
    return await c.send_message("me", text)


def _backup_flag_cmd(key, label):
    async def f(c, e, uid, arg):
        v = onoff(arg)
        if v is None:
            on = await db.uflag(uid, key, True)
            return await e.edit(f"{'🟢' if on else '🔴'} {label} الان {'روشن' if on else 'خاموش'}ه\n"
                                f"`.{label} روشن` | `.{label} خاموش`\n📦 ذخیره‌ها توی گپ خصوصی «{BACKUP_TITLE}» میرن، نه پیام‌های شخصی.")
        await db.uset_flag(uid, key, v)
        note = ""
        if v:
            note = (f"\n📦 گپ «{BACKUP_TITLE}» آماده‌ست" if await ensure_backup(uid)
                    else f"\n⚠️ ساخت گپ «{BACKUP_TITLE}» نشد؛ فعلاً توی Saved Messages ذخیره میشه")
        await e.edit(f"{'🟢' if v else '🔴'} {label} {'روشن' if v else 'خاموش'} شد{note}")
    return f


# ---------------- auto text style (every outgoing message) ----------------
STYLE_ORDER = ["بولد", "ایتالیک", "زیرخط", "خطخورده", "اسپویلر", "مونو"]
STYLE_LABEL = {"بولد": "🅱️ بولد", "ایتالیک": "🔤 ایتالیک", "زیرخط": "زیرخط", "خطخورده": "خط‌خورده",
               "اسپویلر": "🙈 اسپویلر", "مونو": "💻 مونو"}
_PROTECT = re.compile(r"https?://\S+|www\.\S+|\S+@\S+\.\S+|[@#/]\w+|`[^`]*`")


def parse_modes(raw):
    out = []
    for w in re.split(r"[,\s،]+", raw or ""):
        w = norm(w)
        if w in MODES and w not in out:
            out.append(w)
    if "مونو" in out and len(out) > 1:             # code style cannot be combined with the others
        out = ["مونو"]
    return sorted(out, key=STYLE_ORDER.index)


async def get_modes(uid):
    return parse_modes(await db.uget(uid, "textmode", ""))


async def get_autofont(uid):
    v = await db.uget(uid, "autofont", "")
    return v if v in utils.FONTS else ""


async def toggle_mode(uid, name):
    cur = await get_modes(uid)
    if name == "مونو":
        new = [] if "مونو" in cur else ["مونو"]
    else:
        cur = [m for m in cur if m != "مونو"]
        new = [m for m in cur if m != name] if name in cur else cur + [name]
    new = sorted(new, key=STYLE_ORDER.index)
    await db.uset(uid, "textmode", ",".join(new))
    return new


def apply_font(text, ents, font):
    """Convert latin letters to the chosen font (links/@mentions/#tags/commands untouched) and remap entities."""
    f = utils.FONTS.get(font)
    if not f or not re.search(r"[A-Za-z]", text):
        return text, ents
    prot = [(m.start(), m.end()) for m in _PROTECT.finditer(text)]
    out, m16, o16, n16 = [], {}, 0, 0
    for i, ch in enumerate(text):
        m16[o16] = n16
        new = ch if any(a <= i < b for a, b in prot) else f(ch)
        out.append(new)
        o16 += 1 if ord(ch) < 0x10000 else 2
        n16 += 1 if ord(new) < 0x10000 else 2
    m16[o16] = n16
    res = []
    for en in ents:
        a, b = en.offset, en.offset + en.length
        if a in m16 and b in m16:
            en = copy.copy(en)
            en.offset, en.length = m16[a], m16[b] - m16[a]
        res.append(en)
    return "".join(out), res


async def mode_status(uid):
    modes, font = await get_modes(uid), await get_autofont(uid)
    if not modes and not font:
        return ("🎨 **حالت خودکار متن: خاموش 🔴**\nبرای روشن کردن: `.حالت متن بولد ایتالیک` | `.حالت فونت 3`\n"
                "یا از ربات: 📖 همه‌ی دستورها ← ✍️ متن و فونت ← 🎨 حالت خودکار")
    parts = ([" + ".join(STYLE_LABEL[m] for m in modes)] if modes else []) + ([f"فونت {font}: {utils.FONTS[font]('Hello')}"] if font else [])
    return "🎨 **حالت خودکار متن: روشن 🟢**\n" + "\n".join(parts) + "\nخاموش کردن: `.حالت خاموش`"


def _flag_cmd(key, label):
    async def f(c, e, uid, arg):
        v = onoff(arg)
        if v is None:
            return await e.edit(f"`.{label} روشن` یا `.{label} خاموش`")
        await db.uset_flag(uid, key, v)
        await e.edit(f"{'🟢' if v else '🔴'} {label} {'روشن' if v else 'خاموش'} شد")
    return f


async def c_react(c, e, uid, arg):
    a = arg.strip()
    v = onoff(a)
    if v is not None:
        await db.uset_flag(uid, "autoreact", v)
        return await e.edit(f"{'🟢' if v else '🔴'} ری‌اکشن خودکار {'روشن' if v else 'خاموش'} شد")
    if a:
        await db.uset(uid, "react_emoji", a[:8])
        await db.uset_flag(uid, "autoreact", True)
        return await e.edit(f"🟢 ری‌اکشن خودکار روشن شد: {a[:8]}")
    await e.edit("`.ریاکشن روشن` | `.ریاکشن خاموش` | `.ریاکشن 🔥` (انتخاب ایموجی)")


async def c_mute(c, e, uid, arg):
    t = await _target_arg(c, e, arg)
    if not t:
        return await e.edit("توی پیوی بزن، یا توی گروه روی پیام کاربر ریپلای کن، یا `.سکوت آیدی`")
    if t == uid:
        return await e.edit("⚠️ نمیشه خودت رو سکوت کنی")
    await db.uset(uid, f"mute:{t}", "1")
    await e.edit(f"🔇 کاربر `{t}` سکوت شد؛ پیام‌هاش خودکار حذف میشه\nبرای برداشتن: `.لغو_سکوت` (همین‌جا) یا `.لغو_سکوت {t}`")


async def c_unmute(c, e, uid, arg):
    a = utils.to_en_digits(arg.strip())
    if norm(a) in ("همه", "all"):
        rows = await db.uprefix(uid, "mute:")
        for r in rows:
            await db.udel(uid, r["key"])
        return await e.edit(f"🔊 سکوت {len(rows)} کاربر برداشته شد")
    t = await _target_arg(c, e, arg)
    if not t:
        return await e.edit("توی پیوی بزن، یا ریپلای کن، یا `.لغو_سکوت آیدی` | `.لغو_سکوت همه`")
    if await db.uget(uid, f"mute:{t}") is None:
        return await e.edit("این کاربر سکوت نبود (برای دیدن لیست: `.لیست_سکوت`)")
    await db.udel(uid, f"mute:{t}")
    await e.edit(f"🔊 سکوت `{t}` برداشته شد")


async def c_mutelist(c, e, uid, arg):
    rows = await db.uprefix(uid, "mute:")
    lines = []
    for r in rows:
        k = int(r["key"][5:])
        try:
            u = await c.get_entity(k)
            name = utils.clean_name(getattr(u, "first_name", "") or "") or "-"
        except Exception:
            name = "؟"
        lines.append(f"• {name} — `{k}`")
    await e.edit("🔇 **لیست سکوت**\n" + ("\n".join(lines) or "خالیه") +
                 ("\n\nبرداشتن: `.لغو_سکوت آیدی` | `.لغو_سکوت همه`" if lines else ""))


async def c_backup(c, e, uid, arg):
    n = min(int(utils.to_en_digits(arg)), 2000) if utils.to_en_digits(arg).isdigit() else 300
    await e.edit("⏳ در حال بکاپ‌گیری...")
    lines = []
    async for m in c.iter_messages(e.chat_id, limit=n):
        who = "من" if m.out else utils.clean_name(getattr(await m.get_sender(), "first_name", "") or str(m.sender_id))
        lines.append(f"[{m.date.astimezone(TZ):%Y-%m-%d %H:%M}] {who}: {m.raw_text or ('[مدیا]' if m.media else '')}")
    bio = io.BytesIO("\n".join(reversed(lines)).encode())
    bio.name = f"backup_{e.chat_id}.txt"
    await c.send_file("me", bio, caption=f"💾 بکاپ {len(lines)} پیام")
    await e.edit(f"✅ بکاپ {len(lines)} پیام توی Saved Messages ذخیره شد")


async def c_tag(c, e, uid, arg):
    if e.is_private:
        return await e.edit("فقط توی گروه")
    n = min(int(utils.to_en_digits(arg)), 30) if utils.to_en_digits(arg).isdigit() else 10
    users = [u for u in await c.get_participants(e.chat_id, limit=n + 5) if not u.bot and not u.is_self][:n]
    await e.delete()
    for k in range(0, len(users), 5):
        chunk = users[k:k + 5]
        await c.send_message(e.chat_id, " ".join(f"[{utils.clean_name(u.first_name or 'کاربر')}](tg://user?id={u.id})" for u in chunk))
        await asyncio.sleep(1.2)


async def c_ban(c, e, uid, arg):
    t = await _target(e)
    if not t or e.is_private:
        return await e.edit("توی گروه روی پیام کاربر ریپلای کن (باید ادمین باشی)")
    await c.edit_permissions(e.chat_id, t, view_messages=False)
    await e.edit("⛔ کاربر بن شد")


async def c_unban(c, e, uid, arg):
    t = await _target(e)
    if not t or e.is_private:
        return await e.edit("توی گروه روی پیام کاربر ریپلای کن")
    await c.edit_permissions(e.chat_id, t, view_messages=True)
    await e.edit("✅ بن برداشته شد")


async def c_kick(c, e, uid, arg):
    t = await _target(e)
    if not t or e.is_private:
        return await e.edit("توی گروه روی پیام کاربر ریپلای کن")
    await c.kick_participant(e.chat_id, t)
    await e.edit("👢 کاربر اخراج شد")


MODES = {"بولد": types.MessageEntityBold, "ایتالیک": types.MessageEntityItalic, "مونو": types.MessageEntityCode,
         "اسپویلر": types.MessageEntitySpoiler, "خطخورده": types.MessageEntityStrike,
         "زیرخط": types.MessageEntityUnderline}


async def c_mode(c, e, uid, arg):
    """`.حالت متن بولد ایتالیک` | `.حالت فونت 3` | `.حالت خاموش` | `.حالت وضعیت`"""
    parts = arg.split()
    head = norm(parts[0]) if parts else "وضعیت"
    if head in ("وضعیت", "status"):
        return await e.edit(await mode_status(uid))
    if head in ("خاموش", "off"):
        await db.uset(uid, "textmode", "")
        await db.uset(uid, "autofont", "")
        return await e.edit("🔴 حالت خودکار متن خاموش شد (سبک و فونت)")
    if head == "فونت":
        v = utils.to_en_digits(parts[1]) if len(parts) > 1 else ""
        if norm(v) in ("خاموش", "off", "0"):
            await db.uset(uid, "autofont", "")
            return await e.edit("🔴 فونت خودکار خاموش شد")
        if v not in utils.FONTS:
            lines = "\n".join(f"{k}: {f('Hello')}" for k, f in utils.FONTS.items())
            return await e.edit("🔠 **فونت خودکار**\n" + lines + "\nمثال: `.حالت فونت 3` | خاموش: `.حالت فونت خاموش`")
        await db.uset(uid, "autofont", v)
        return await e.edit(f"🟢 فونت خودکار {v} روشن شد: {utils.FONTS[v]('Hello')}\nاز الان حروف انگلیسی همه‌ی پیام‌هات با این فونت ارسال میشه")
    if head == "متن" and len(parts) > 1:
        if norm(parts[1]) in ("خاموش", "off"):
            await db.uset(uid, "textmode", "")
            return await e.edit("🔴 سبک خودکار متن خاموش شد")
        modes = parse_modes(" ".join(parts[1:]))
        if not modes:
            return await e.edit("سبک نامعتبر. سبک‌ها: بولد، ایتالیک، زیرخط، خط_خورده، اسپویلر، مونو")
        await db.uset(uid, "textmode", ",".join(modes))
        return await e.edit("🟢 سبک خودکار: " + " + ".join(STYLE_LABEL[m] for m in modes)
                            + "\nاز الان همه‌ی پیام‌هات خودکار همین‌طوری ویرایش میشن")
    await e.edit("🎨 **حالت خودکار متن**\n`.حالت متن بولد` — یک سبک\n`.حالت متن بولد ایتالیک` — چند سبک با هم\n"
                 "`.حالت فونت 3` — فونت انگلیسی (۱ تا ۶)\n`.حالت خاموش` — خاموش کردن همه\n`.حالت وضعیت`\n"
                 "سبک‌ها: بولد، ایتالیک، زیرخط، خط_خورده، اسپویلر، مونو (مونو با بقیه ترکیب نمیشه)")


COMMANDS = {
    "power": c_power, "panel": c_panel, "help": c_help, "status": c_status,
    "ping": c_ping, "time": c_time, "calc": c_calc, "id": c_id, "info": c_info,
    "bold": _style("**{}**"), "italic": _style("__{}__"), "mono": _style("`{}`"),
    "spoiler": _style("||{}||"), "strike": _style("~~{}~~"),
    "font": c_font, "type": c_type, "tr": c_tr, "tts": c_tts,
    "price": c_price, "weather": c_weather, "owghat": c_owghat, "dl": c_dl,
    "save": c_save, "del": c_del, "pin": c_pin, "unpin": c_unpin,
    "block": c_block, "unblock": c_unblock, "stats": c_stats,
    "afk": c_afk, "afktext": c_afktext, "kw": c_kw, "clock": c_clock, "clockpic": c_clockpic,
    "remind": c_remind, "pass": c_pass, "rand": c_rand, "choose": c_choose,
    "dice": _dice("🎲"), "dart": _dice("🎯"), "ball": _dice("🏀"),
    "bal": c_bal, "top": c_top, "buy": c_buy, "give": c_give,
    "later": c_later, "cd": c_cd, "qr": c_qr, "typing": c_typing, "purge": c_purge,
    "bio": c_bio, "setname": c_setname, "note": c_note, "coin": c_coin, "reverse": c_reverse,
    "me": c_me, "copy": c_copy, "selfdestruct": c_selfdestruct, "invite": c_invite, "date": c_date, "hack": c_hack, "love": c_love, "8ball": c_8ball,
    "spam": c_spam, "spamstop": c_spamstop, "antidel": _backup_flag_cmd("antidel", "ضدحذف"), "ttl": _backup_flag_cmd("ttl", "تایمردار"), "register": c_register, "listcmd": c_list_cmd, "autoseen": _flag_cmd("autoseen", "سین_خودکار"),
    "react": c_react, "lockpv": _flag_cmd("lockpv", "قفل_پیوی"), "mute": c_mute, "unmute": c_unmute,
    "mutelist": c_mutelist, "backup": c_backup, "tag": c_tag, "ban": c_ban, "unban": c_unban, "kick": c_kick,
    "mode": c_mode,
}

# Persian command name -> internal key (English names keep working as hidden aliases)
FA = {
    "مدیریت_سلف": "power", "پنل": "panel", "راهنما": "help", "وضعیت": "status", "پینگ": "ping", "زمان": "time",
    "حساب": "calc", "آیدی": "id", "اطلاعات": "info", "بولد": "bold", "ایتالیک": "italic", "مونو": "mono",
    "اسپویلر": "spoiler", "خط_خورده": "strike", "فونت": "font", "تایپ": "type", "ترجمه": "tr", "صدا": "tts",
    "قیمت": "price", "هوا": "weather", "اوقات": "owghat", "دانلود": "dl", "ذخیره": "save", "حذف": "del",
    "پین": "pin", "حذف_پین": "unpin", "بلاک": "block", "آنبلاک": "unblock", "آمار": "stats", "منشی": "afk",
    "متن_منشی": "afktext", "کلمه": "kw", "ساعت": "clock", "یادآور": "remind", "رمز": "pass", "تصادفی": "rand",
    "انتخاب": "choose", "تاس": "dice", "دارت": "dart", "بسکتبال": "ball", "موجودی": "bal",
    "برترین": "top", "خرید": "buy", "انتقال": "give", "زمانبندی": "later", "شمارش": "cd", "کیوآر": "qr",
    "درحال_تایپ": "typing", "پاکسازی": "purge", "بیو": "bio", "اسم": "setname", "تاریخ": "date", "هک": "hack",
    "عشق": "love", "بپرس": "8ball", "یادداشت": "note", "سکه": "coin", "معکوس": "reverse", "من": "me",
    "کپی": "copy", "تخریب": "selfdestruct", "دعوت": "invite",
    "اسپم": "spam", "توقف_اسپم": "spamstop", "ضدحذف": "antidel", "تایمردار": "ttl", "ثبت": "register", "لیست": "listcmd", "سین_خودکار": "autoseen", "سین": "autoseen",
    "ریاکشن": "react", "ری_اکشن": "react", "قفل_پیوی": "lockpv", "سکوت": "mute", "لغو_سکوت": "unmute",
    "لیست_سکوت": "mutelist", "بکاپ": "backup", "تگ": "tag", "بن": "ban", "آزاد": "unban", "اخراج": "kick",
    "حالت": "mode",
}
# Long, self-explanatory names (same commands; the short ones above keep working)
FA_LONG = {
    "تبدیل_متن_به_ویس": "tts", "ویس": "tts", "ترجمه_متن": "tr", "ماشین_حساب": "calc", "قیمت_ارز": "price",
    "آب_و_هوا": "weather", "اوقات_شرعی": "owghat", "دانلود_ویدیو": "dl", "ساخت_کیوآر": "qr", "ساخت_رمز": "pass",
    "عدد_تصادفی": "rand", "انتخاب_تصادفی": "choose", "ساعت_زنده": "clock", "ساعت_عکس": "clockpic", "ساعت_پروفایل": "clockpic", "ساعت_روی_عکس": "clockpic", "ساعت_شمسی": "time",
    "منشی_خودکار": "afk", "ریاکشن_خودکار": "react", "تغییر_بیو": "bio", "تغییر_اسم": "setname",
    "اطلاعات_من": "me", "اطلاعات_کاربر": "info", "آیدی_چت": "id", "آمار_چت": "stats", "ذخیره_پیام": "save",
    "حذف_پیام": "del", "شمارش_معکوس": "cd", "ارسال_زمانبندی": "later", "پیام_خودتخریب": "selfdestruct",
    "بکاپ_چت": "backup", "تگ_اعضا": "tag", "بن_کردن": "ban", "رفع_بن": "unban", "بلاک_کاربر": "block",
    "رفع_بلاک": "unblock", "سکوت_کاربر": "mute", "خرید_الماس": "buy", "انتقال_الماس": "give",
    "دعوت_دوستان": "invite", "جدول_برترین": "top", "موجودی_الماس": "bal", "روشن_خاموش_سلف": "power",
}
ALIAS = {norm(k): v for k, v in {**FA, **FA_LONG}.items()}


def resolve(cmd: str):
    k = norm(cmd)
    return ALIAS.get(k) or (k if k in COMMANDS else None)


def _media_key(msg):
    """Stable id of the photo/document in a message (None when there is no real media) - detects edited media."""
    m = getattr(msg, "media", None)
    if m is None or getattr(m, "webpage", None):
        return None
    for attr in ("photo", "document"):
        o = getattr(m, attr, None)
        if o is not None and getattr(o, "id", None) is not None:
            return f"{attr}:{o.id}"
    return type(m).__name__


# ---------------- event wiring ----------------
def attach(c: TelegramClient, uid: int):
    cache: dict[int, tuple] = {}
    afk_seen: dict[int, float] = {}
    kw_seen: dict[int, float] = {}

    @c.on(events.NewMessage(outgoing=True))
    async def on_out(e):
        m = re.match(r"^\.(\S+)\s*(.*)$", e.raw_text or "", re.S)
        cmd, arg = (resolve(m.group(1)), m.group(2).strip()) if m else (None, "")
        fn = COMMANDS.get(cmd) if cmd else None
        if not fn:
            return await plain_edit(c, e, uid)
        if cmd != "power" and cmd != "panel" and not await db.uflag(uid, "selfon", True):
            return
        if cmd not in FREE and not await db.user_is_active(uid):
            return await e.edit("⛔ الماس‌هات تموم شده و سلف متوقفه. از ربات مدیریت الماس بخر یا کد هدیه بزن؛ بعدش خودکار ادامه میده.")
        try:
            await fn(c, e, uid, arg)
        except Exception as ex:
            log.exception("command %s failed", cmd)
            try:
                await e.edit(f"⚠️ خطا: {type(ex).__name__}")
            except Exception:
                pass

    async def plain_edit(c, e, uid):
        """Non-command outgoing message: pending emoji registration, premium-emoji swap, auto text style."""
        if not await db.uflag(uid, "selfon", True) or not await db.user_is_active(uid):
            return
        text = e.raw_text or ""
        if not text or e.message.fwd_from or getattr(e.message, "via_bot_id", None):
            return
        st = reg_state.get(uid)
        if st and time.time() - st[1] < 120 and len(text.strip()) <= 12 and not custom_ids(e.message):
            reg_state.pop(uid, None)
            free = text.strip()
            await save_emoji(uid, free, st[0])
            await e.edit(f"✅ ثبت شد: {free} ➜ ایموجی پریمیوم\nاز الان هرجا {free} بفرستی تبدیل میشه.")
            await asyncio.sleep(3)
            return await e.delete()
        emap = await emoji_map(uid)
        modes = await get_modes(uid)
        font = await get_autofont(uid)
        if not emap and not modes and not font:
            return
        ents = list(e.message.entities or [])
        taken = [(en.offset, en.offset + en.length) for en in ents if isinstance(en, types.MessageEntityCustomEmoji)]
        new = []
        for free, doc in emap.items():
            pos = 0
            while (i := text.find(free, pos)) != -1:
                off, ln = u16(text[:i]), u16(free)
                if not any(a < off + ln and off < b for a, b in taken):
                    new.append(types.MessageEntityCustomEmoji(offset=off, length=ln, document_id=doc))
                    taken.append((off, off + ln))
                pos = i + len(free)
        is_cmd_like = text.lstrip().startswith("/")              # bot commands must stay untouched
        own_code = any(isinstance(en, (types.MessageEntityCode, types.MessageEntityPre)) for en in ents)
        if modes and not is_cmd_like and not own_code:
            total = u16(text)
            for name in modes:
                cls = MODES[name]
                if not any(type(en) is cls and en.offset == 0 and en.length >= total for en in ents):
                    new.append(cls(offset=0, length=total))
        all_ents, out_text = ents + new, text
        if font and not is_cmd_like:
            out_text, all_ents = apply_font(text, all_ents, font)
        if not new and out_text == text:
            return
        has_emoji = any(isinstance(n, types.MessageEntityCustomEmoji) for n in new)
        if has_emoji and not await is_premium(c, uid):
            # free account: the server would drop the custom emoji, so send it through the bot instead
            if await send_via_bot(c, e, uid, out_text, all_ents):
                return
            if uid not in _inline_warned:
                _inline_warned.add(uid)
                try:
                    await c.send_message("me", "⚠️ ایموجی پریمیوم برای اکانت غیرپریمیوم باید از طریق ربات مدیریت ارسال بشه، "
                                               "ولی ارسال انجام نشد. بررسی کن: ۱) Inline Mode ربات روشن باشه "
                                               "۲) ربات یوزرنیم Fragment داشته باشه (شرط تلگرام برای ایموجی پریمیوم توی ربات‌ها).")
                except Exception:
                    pass
        try:
            await e.edit(out_text, formatting_entities=all_ents, parse_mode=None)
        except errors.MessageNotModifiedError:
            pass

    @c.on(events.NewMessage(incoming=True))
    async def on_in(e):
        if not await db.user_is_active(uid) or not await db.uflag(uid, "selfon", True):
            return
        try:
            sender = await e.get_sender()
        except Exception:
            sender = None
        if getattr(sender, "bot", False):
            return
        if not e.is_private and e.sender_id and await db.uget(uid, f"mute:{e.sender_id}"):
            try:
                return await c.delete_messages(e.chat_id, [e.id], revoke=True)   # needs admin rights in the group
            except Exception:
                pass
        if e.is_private and e.sender_id != 777000:
            # privacy tools: mute list, private lock (non-contacts), auto seen, auto reaction
            try:
                if await db.uget(uid, f"mute:{e.sender_id}") or (
                        await db.uflag(uid, "lockpv") and not getattr(sender, "contact", False)):
                    return await c.delete_messages(e.chat_id, [e.id], revoke=True)
                if await db.uflag(uid, "autoseen"):
                    await c.send_read_acknowledge(e.chat_id, e.message)
                if await db.uflag(uid, "autoreact"):
                    await c(functions.messages.SendReactionRequest(
                        peer=await e.get_input_chat(), msg_id=e.id,
                        reaction=[types.ReactionEmoji(emoticon=await db.uget(uid, "react_emoji", "❤️"))]))
            except Exception as ex:
                log.warning("privacy tools: %s", ex)
        name = utils.clean_name(getattr(sender, "first_name", "") or str(e.sender_id))

        if e.is_private:
            # remember for anti-delete / anti-edit
            if await db.uflag(uid, "antidel", True) or await db.uflag(uid, "ttl", True):
                cache[e.id] = (name, e.raw_text or ("[مدیا]" if e.media else ""), _media_key(e.message),
                               e.sender_id, getattr(sender, "username", None))
                if len(cache) > 6000:
                    cache.pop(next(iter(cache)))
            # save self-destructing media
            if await db.uflag(uid, "ttl", True) and getattr(e.message.media, "ttl_seconds", None):
                d = tempfile.mkdtemp()
                try:
                    path = await e.message.download_media(file=d + "/")
                    if path:
                        uname = getattr(sender, "username", None)
                        await backup_send(c, uid, file=path,
                                          caption=f"⏳ تایمردار از {name}\n🆔 {e.sender_id}" + (f" | @{uname}" if uname else ""))
                except Exception:
                    log.exception("ttl save failed")
                finally:
                    shutil.rmtree(d, ignore_errors=True)
            # keyword replies
            text = (e.raw_text or "").lower()
            if text and await db.uflag(uid, "kw", True) and time.time() - kw_seen.get(e.chat_id, 0) > 5:
                for r in await db.ar_list(uid):
                    if r["keyword"] in text:
                        kw_seen[e.chat_id] = time.time()
                        return await e.reply(r["response"])
            # afk
            if await db.uflag(uid, "afk") and time.time() - afk_seen.get(e.sender_id, 0) > 3600:
                afk_seen[e.sender_id] = time.time()
                await e.reply(await db.uget(uid, "afk_text", "سلام 👋 الان آفلاینم، به محض اینکه آنلاین شدم جواب می‌دم."))

    @c.on(events.MessageDeleted())
    async def on_deleted(e):
        if not await db.user_is_active(uid) or not await db.uflag(uid, "selfon", True):
            return
        notify = await db.uflag(uid, "antidel", True)
        for i in e.deleted_ids:
            if i in cache:
                item = cache.pop(i)
                if notify:
                    await backup_send(c, uid, text=f"🗑 **پیام حذف شد**\n👤 {item[0]}\n💬 {item[1]}")

    @c.on(events.MessageEdited(incoming=True))
    async def on_edited(e):
        """Edited private messages -> backup group (works with «ضد حذف» OR «ذخیره تایمردار و ادیت‌شده‌ها»)."""
        if not e.is_private or not await db.user_is_active(uid) or not await db.uflag(uid, "selfon", True):
            return
        if not (await db.uflag(uid, "antidel", True) or await db.uflag(uid, "ttl", True)):
            return
        if e.sender_id == 777000:
            return
        old = cache.get(e.id)
        new = e.raw_text or ("[مدیا]" if e.media else "")
        mk = _media_key(e.message)
        if old and old[1] == new and old[2] == mk:
            return                                           # nothing visible changed (reaction, read mark...)
        if old:
            name, before, old_mk, sid, uname = old
        else:
            try:
                sender = await e.get_sender()
            except Exception:
                sender = None
            name = utils.clean_name(getattr(sender, "first_name", "") or str(e.sender_id))
            before, old_mk, sid, uname = None, None, e.sender_id, getattr(sender, "username", None)
        when = datetime.now(TZ).strftime("%H:%M:%S")
        who = f"👤 {name}\n🆔 {sid}" + (f" | @{uname}" if uname else "")
        if before is None:
            body = f"قبل: (ثبت نشده بود؛ قبل از روشن شدن سلف یا ری‌استارت)\nبعد: {new}"
        else:
            body = f"قبل: {before}\nبعد: {new}"
        msg = f"✏️ **پیام ادیت شد** ({when})\n{who}\n{body}"[:4000]
        await backup_send(c, uid, text=msg)
        if mk and mk != old_mk and not getattr(e.message.media, "webpage", None):
            d = tempfile.mkdtemp()                           # the edit replaced the media: keep the new file too
            try:
                path = await e.message.download_media(file=d + "/")
                if path:
                    await backup_send(c, uid, file=path, caption=f"✏️ مدیای جدیدِ پیام ادیت‌شده\n{who}")
            except Exception:
                log.exception("edited media save failed")
            finally:
                shutil.rmtree(d, ignore_errors=True)
        cache[e.id] = (name, new, mk, sid, uname)


async def clock_loop(c, uid):
    """Sleeps until the next minute boundary, then pushes the new time immediately."""
    while True:
        try:
            now = datetime.now(TZ)
            await asyncio.sleep(60 - now.second - now.microsecond / 1e6 + 0.05)
            if not await db.user_is_active(uid) or not await db.uflag(uid, "selfon", True):
                continue
            if not any([await db.uflag(uid, f) for f, _ in CLOCK_TARGETS.values()]):
                continue
            await push_clock(c, uid)
        except asyncio.CancelledError:
            raise
        except errors.FloodWaitError as ex:
            await asyncio.sleep(ex.seconds + 1)
        except Exception as ex:
            log.warning("clock %s: %s", uid, ex)
            await asyncio.sleep(5)


# ---------------- lifecycle ----------------
async def start_user(uid: int, session: str, api_id: int, api_hash: str) -> bool:
    if uid in clients:
        await stop_user(uid)
    c = TelegramClient(StringSession(session), api_id, api_hash, connection_retries=-1, retry_delay=1,
                       auto_reconnect=True, flood_sleep_threshold=30, request_retries=5)
    await c.connect()
    try:
        ok = await c.is_user_authorized()
    except Exception:
        ok = False
    if not ok:
        await c.disconnect()
        return False
    attach(c, uid)
    clients[uid] = c
    _tasks[uid] = [asyncio.create_task(clock_loop(c, uid)), asyncio.create_task(clockpic_loop(c, uid))]
    log.info("self started for %s", uid)
    return True


async def stop_user(uid: int):
    for t in _tasks.pop(uid, []):
        t.cancel()
    _cpic_last.pop(uid, None)
    c = clients.pop(uid, None)
    if c:
        try:
            await c.disconnect()
        except Exception:
            pass
