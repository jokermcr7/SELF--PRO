"""The userbot ("self") that runs inside every user's Telegram account."""
import asyncio
import io
import logging
import os
import random
import re
import secrets
import shutil
import tempfile
import time
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp
import jdatetime
from telethon import TelegramClient, events, functions, types
from telethon.sessions import StringSession

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
    ("ttl", "⏳ ذخیره تایمردار", True),
    ("kw", "💬 پاسخ کلمات", True),
    ("clock", "🕒 ساعت توی اسم", False),
]

HELP = """**🤖 راهنمای سلف**

**عمومی**
`.panel` پنل مدیریت | `.status` وضعیت | `.renew` تمدید با ۱۵۰۰ 💎
`.ping` `.time` `.calc 2*(3+4)` `.id` `.info` (ریپلای)

**متن**
`.bold` `.italic` `.mono` `.spoiler` `.strike` + متن
`.font 1..6 متن` (فونت انگلیسی) | `.type متن` تایپ انیمیشنی
`.tr en متن` یا ریپلای + `.tr fa` ترجمه | `.tts fa متن` تبدیل به صدا

**اطلاعات**
`.price btc|eth|ton|eur|gbp...` | `.weather tehran` | `.owghat tehran`

**چت و گروه**
`.save` (ریپلای) ذخیره | `.del 10` حذف ۱۰ پیام خودت
`.pin` `.unpin` | `.block` `.unblock` | `.stats`

**ضد حذف و تایمردار**: پیام حذف/ادیت‌شده‌ی پیوی و عکس/ویدیوی تایمردار میره Saved Messages

**منشی و پاسخ کلمه‌ای**
`.afk on|off` | `.afktext متن`
`.kw add کلمه | پاسخ` | `.kw del کلمه` | `.kw list`

**ساعت**: `.clock on|off` ساعت شمسی کنار اسمت

**ابزار**
`.dl لینک` دانلود ویدیو (یوتیوب و...) | `.remind 10m متن`
`.pass 16` | `.rand 1 100` | `.choose الف|ب|ج` | `.dice` `.dart` `.ball`
"""

# short version: must fit a photo caption (1024 chars) inside the panel
HELP_SHORT = """**🤖 راهنمای سلف**
`.panel` `.status` `.renew` `.ping` `.time` `.calc`
`.bold` `.mono` `.spoiler` `.font` `.type`
`.tr` ترجمه | `.tts` صدا | `.price` ارز | `.weather` | `.owghat`
`.save` `.del` `.pin` `.block` `.stats` `.id` `.info`
`.afk on|off` | `.afktext` | `.kw add|del|list` | `.clock on|off`
`.dl لینک` | `.remind 10m متن` | `.pass` `.rand` `.choose`
لیست کامل: `.help`"""

FREE = {"panel", "help", "status", "renew"}


# ---------------- helpers ----------------
def now_fa() -> str:
    j = jdatetime.datetime.fromgregorian(datetime=datetime.now(TZ))
    return j.strftime("%Y/%m/%d  %H:%M:%S")


async def status_text(uid) -> str:
    left = await db.expires_at(uid) - time.time()
    if left <= 0:
        s = "⛔ منقضی شده"
    else:
        s = f"✅ فعال | {int(left // 86400)} روز و {int(left % 86400 // 3600)} ساعت مانده"
    return f"**📊 وضعیت سلف**\n{s}\n💎 الماس شما: {await db.balance(uid)}\n🕒 {now_fa()}"


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


# ---------------- commands ----------------
async def c_panel(c, e, uid, arg):
    await e.delete()
    res = await c.inline_query(BOT_USERNAME, "panel")
    if res:
        await res[0].click(e.chat_id)
    else:
        await c.send_message("me", "⚠️ پنل باز نشد؛ Inline Mode ربات مدیریت رو چک کن.")


async def c_help(c, e, uid, arg):
    await e.edit(HELP)


async def c_status(c, e, uid, arg):
    await e.edit(await status_text(uid))


async def c_renew(c, e, uid, arg):
    if not await db.renew(uid):
        return await e.edit(f"💎 الماس کافی نیست ({db.DIAMONDS_PER_30_DAYS} لازمه)")
    await e.edit("✅ ۳۰ روز تمدید شد\n" + await status_text(uid))


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
        return await e.edit("🔤 فونت‌ها:\n" + lines + "\nمثال: `.font 1 Hello` یا ریپلای + `.font 2`")
    txt = parts[1] if len(parts) > 1 else await _reply_text(e)
    await e.edit(utils.FONTS[parts[0]](txt) or "—")


async def c_type(c, e, uid, arg):
    if not arg:
        return await e.edit("مثال: `.type سلام`")
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
        return await e.edit("⚠️ عبارت نامعتبره. مثال: `.calc 2*(3+4)`")
    await e.edit(f"🧮 `{arg}` = **{res}**")


async def c_tr(c, e, uid, arg):
    from deep_translator import GoogleTranslator
    parts = arg.split(None, 1)
    lang = parts[0] if parts else "fa"
    text = parts[1] if len(parts) > 1 else await _reply_text(e)
    if not text:
        return await e.edit("مثال: `.tr en سلام` یا ریپلای + `.tr fa`")
    out = await asyncio.to_thread(lambda: GoogleTranslator(source="auto", target=lang).translate(text))
    await e.edit(f"🌐 {out}")


CRYPTO = {"btc": "bitcoin", "eth": "ethereum", "ton": "the-open-network", "trx": "tron",
          "usdt": "tether", "bnb": "binancecoin", "sol": "solana", "xrp": "ripple",
          "doge": "dogecoin", "ltc": "litecoin"}


async def c_price(c, e, uid, arg):
    s = (arg or "btc").lower().strip()
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


async def c_tts(c, e, uid, arg):
    from gtts import gTTS
    lang, text = "fa", arg
    parts = arg.split(None, 1)
    if len(parts) == 2 and parts[0].isascii() and parts[0].isalpha() and len(parts[0]) <= 5:
        lang, text = parts
    text = text or await _reply_text(e)
    if not text:
        return await e.edit("مثال: `.tts fa سلام`")

    def mk():
        bio = io.BytesIO()
        gTTS(text=text, lang=lang).write_to_fp(bio)
        bio.seek(0)
        bio.name = "tts.mp3"
        return bio

    bio = await asyncio.to_thread(mk)
    reply_to = e.message.reply_to_msg_id
    await e.delete()
    await c.send_file(e.chat_id, bio, reply_to=reply_to)


async def c_dl(c, e, uid, arg):
    if not arg.startswith("http"):
        return await e.edit("مثال: `.dl لینک`")
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
    if e.is_reply:
        return (await e.get_reply_message()).sender_id
    if e.is_private:
        return e.chat_id
    return None


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
    if arg not in ("on", "off"):
        return await e.edit("`.afk on` یا `.afk off`")
    await db.uset_flag(uid, "afk", arg == "on")
    await e.edit("😴 منشی خودکار روشن شد" if arg == "on" else "👋 منشی خودکار خاموش شد")


async def c_afktext(c, e, uid, arg):
    if not arg:
        return await e.edit("مثال: `.afktext الان نیستم، بعداً جواب میدم`")
    await db.uset(uid, "afk_text", arg)
    await e.edit("✅ متن منشی ذخیره شد")


async def c_kw(c, e, uid, arg):
    parts = arg.split(None, 1)
    sub = parts[0] if parts else "list"
    rest = parts[1] if len(parts) > 1 else ""
    if sub == "add" and "|" in rest:
        k, r = [x.strip() for x in rest.split("|", 1)]
        if not k or not r:
            return await e.edit("مثال: `.kw add سلام | سلام عزیزم`")
        await db.ar_add(uid, k, r)
        return await e.edit(f"✅ «{k}» اضافه شد")
    if sub == "del" and rest:
        await db.ar_del(uid, rest.strip())
        return await e.edit("🗑 حذف شد")
    rows = await db.ar_list(uid)
    await e.edit("💬 **پاسخ کلمات**\n" + ("\n".join(f"• {r['keyword']} ← {r['response']}" for r in rows) or "خالیه"))


async def set_clock(uid, on):
    c = clients.get(uid)
    if not c:
        return
    if on:
        me = await c.get_me()
        base = re.sub(r"\s*\|\s*\d{2}:\d{2}$", "", me.first_name or "")
        await db.uset(uid, "base_name", base)
        await db.uset_flag(uid, "clock", True)
    else:
        base = await db.uget(uid, "base_name")
        await db.uset_flag(uid, "clock", False)
        if base:
            await c(functions.account.UpdateProfileRequest(first_name=base))


async def c_clock(c, e, uid, arg):
    if arg not in ("on", "off"):
        return await e.edit("`.clock on` یا `.clock off`")
    await set_clock(uid, arg == "on")
    await e.edit("🕒 ساعت روشن شد (هر دقیقه آپدیت میشه)" if arg == "on" else "🕒 ساعت خاموش شد")


async def c_remind(c, e, uid, arg):
    m = re.match(r"^(\d+)\s*([smh])\s+(.+)$", utils.to_en_digits(arg), re.S)
    if not m:
        return await e.edit("مثال: `.remind 10m تماس بگیر`")
    secs = int(m[1]) * {"s": 1, "m": 60, "h": 3600}[m[2]]
    if secs > 86400:
        return await e.edit("حداکثر ۲۴ ساعت")
    await e.edit(f"⏰ یادآوری تنظیم شد ({m[1]}{m[2]})")

    async def later():
        await asyncio.sleep(secs)
        await c.send_message("me", f"⏰ یادآوری: {m[3]}")

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
        return await e.edit("مثال: `.choose الف|ب|ج`")
    await e.edit(f"🎯 {random.choice(opts)}")


def _dice(emoji):
    async def f(c, e, uid, arg):
        await e.delete()
        await c.send_message(e.chat_id, file=types.InputMediaDice(emoji))
    return f


COMMANDS = {
    "panel": c_panel, "help": c_help, "status": c_status, "renew": c_renew,
    "ping": c_ping, "time": c_time, "calc": c_calc, "id": c_id, "info": c_info,
    "bold": _style("**{}**"), "italic": _style("__{}__"), "mono": _style("`{}`"),
    "spoiler": _style("||{}||"), "strike": _style("~~{}~~"),
    "font": c_font, "type": c_type, "tr": c_tr, "tts": c_tts,
    "price": c_price, "weather": c_weather, "owghat": c_owghat, "dl": c_dl,
    "save": c_save, "del": c_del, "pin": c_pin, "unpin": c_unpin,
    "block": c_block, "unblock": c_unblock, "stats": c_stats,
    "afk": c_afk, "afktext": c_afktext, "kw": c_kw, "clock": c_clock,
    "remind": c_remind, "pass": c_pass, "rand": c_rand, "choose": c_choose,
    "dice": _dice("🎲"), "dart": _dice("🎯"), "ball": _dice("🏀"),
}


# ---------------- event wiring ----------------
def attach(c: TelegramClient, uid: int):
    cache: dict[int, tuple] = {}
    afk_seen: dict[int, float] = {}
    kw_seen: dict[int, float] = {}

    @c.on(events.NewMessage(outgoing=True))
    async def on_out(e):
        m = re.match(r"^\.(\S+)\s*(.*)$", e.raw_text or "", re.S)
        if not m:
            return
        cmd, arg = m.group(1).lower(), m.group(2).strip()
        fn = COMMANDS.get(cmd)
        if not fn:
            return
        if cmd not in FREE and not await db.user_is_active(uid):
            return await e.edit("⛔ اشتراک سلف تموم شده. با `.renew` (۱۵۰۰ 💎) تمدید کن.")
        try:
            await fn(c, e, uid, arg)
        except Exception as ex:
            log.exception("command %s failed", cmd)
            try:
                await e.edit(f"⚠️ خطا: {type(ex).__name__}")
            except Exception:
                pass

    @c.on(events.NewMessage(incoming=True))
    async def on_in(e):
        if not await db.user_is_active(uid):
            return
        try:
            sender = await e.get_sender()
        except Exception:
            sender = None
        if getattr(sender, "bot", False):
            return
        name = utils.clean_name(getattr(sender, "first_name", "") or str(e.sender_id))

        if e.is_private:
            # remember for anti-delete / anti-edit
            if await db.uflag(uid, "antidel", True):
                cache[e.id] = (name, e.raw_text or ("[مدیا]" if e.media else ""))
                if len(cache) > 3000:
                    cache.pop(next(iter(cache)))
            # save self-destructing media
            if await db.uflag(uid, "ttl", True) and getattr(e.message.media, "ttl_seconds", None):
                d = tempfile.mkdtemp()
                try:
                    path = await e.message.download_media(file=d + "/")
                    if path:
                        await c.send_file("me", path, caption=f"⏳ تایمردار از {name}")
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
        if not await db.user_is_active(uid):
            return
        for i in e.deleted_ids:
            if i in cache:
                name, txt = cache.pop(i)
                await c.send_message("me", f"🗑 **پیام حذف شد**\n👤 {name}\n💬 {txt}")

    @c.on(events.MessageEdited(incoming=True))
    async def on_edited(e):
        if not e.is_private or not await db.user_is_active(uid):
            return
        if not await db.uflag(uid, "antidel", True):
            return
        old = cache.get(e.id)
        new = e.raw_text or ""
        if old and old[1] != new:
            await c.send_message("me", f"✏️ **پیام ادیت شد**\n👤 {old[0]}\nقبل: {old[1]}\nبعد: {new}")
        if old:
            cache[e.id] = (old[0], new)


async def clock_loop(c, uid):
    last = ""
    while True:
        try:
            await asyncio.sleep(30)
            if not await db.uflag(uid, "clock") or not await db.user_is_active(uid):
                continue
            hm = datetime.now(TZ).strftime("%H:%M")
            if hm == last:
                continue
            base = await db.uget(uid, "base_name", "")
            await c(functions.account.UpdateProfileRequest(first_name=f"{base} | {hm}"[:64]))
            last = hm
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            log.warning("clock %s: %s", uid, ex)
            await asyncio.sleep(60)


# ---------------- lifecycle ----------------
async def start_user(uid: int, session: str, api_id: int, api_hash: str) -> bool:
    if uid in clients:
        await stop_user(uid)
    c = TelegramClient(StringSession(session), api_id, api_hash)
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
    _tasks[uid] = [asyncio.create_task(clock_loop(c, uid))]
    log.info("self started for %s", uid)
    return True


async def stop_user(uid: int):
    for t in _tasks.pop(uid, []):
        t.cancel()
    c = clients.pop(uid, None)
    if c:
        try:
            await c.disconnect()
        except Exception:
            pass
