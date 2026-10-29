"""The userbot ("self") running inside each user's account: panel, live clock, premium emoji."""
import asyncio
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from telethon import TelegramClient, errors, events, functions, types
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityCustomEmoji

import db

log = logging.getLogger("selfbot")
TZ = ZoneInfo("Asia/Tehran")
BOT_USERNAME = ""  # set from main.py

clients: dict[int, TelegramClient] = {}
_tasks: dict[int, asyncio.Task] = {}
FE0F = "️"

TOGGLES = [("pemoji", "✨ ایموجی پرمیوم", True), ("clock", "🕒 ساعت زنده", False)]

# ---------------- clock fonts ----------------
def _shift(base):
    return lambda d: chr(base + d)


_CIRCLED = "⓪①②③④⑤⑥⑦⑧⑨"
_SUP = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_SUB = "₀₁₂₃₄₅₆₇₈₉"
_NEG = "⓿❶❷❸❹❺❻❼❽❾"
FONTS = [
    ("عادی", lambda d: str(d)),
    ("بولد", _shift(0x1D7CE)),
    ("توخالی", _shift(0x1D7D8)),
    ("سنس", _shift(0x1D7E2)),
    ("سنس بولد", _shift(0x1D7EC)),
    ("مونو", _shift(0x1D7F6)),
    ("فول‌وید", _shift(0xFF10)),
    ("دایره‌ای", lambda d: _CIRCLED[d]),
    ("دایره‌ی توپر", lambda d: _NEG[d]),
    ("بالانویس", lambda d: _SUP[d]),
    ("پایین‌نویس", lambda d: _SUB[d]),
    ("فارسی", _shift(0x06F0)),
]
LOCS = ["name", "bio", "both"]
LOC_LABEL = {"name": "اسم", "bio": "بیو", "both": "اسم و بیو"}


def style_time(hm: str, font: int) -> str:
    fn = FONTS[font % len(FONTS)][1]
    return "".join(fn(int(ch)) if ch.isdigit() else ch for ch in hm)


def hm_at(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%H:%M")


HELP = """**🤖 راهنمای سلف**

`.panel` پنل مدیریت | `.status` وضعیت

**🕒 ساعت زنده** (دقیقاً سر هر دقیقه آپدیت میشه)
`.clock on|off` | `.clock name|bio|both` (جای ساعت)
`.clockfont` لیست فونت‌ها | `.clockfont 3` انتخاب فونت

**✨ ایموجی پرمیوم**
۱) پیام بفرست: `.pe add` + ایموجی پرمیوم‌هات
۲) دلخواه: `.pe add 😀 ✨پرمیوم`
۳) یا ریپلای روی پیام دارای ایموجی پرمیوم + `.pe add`
از اون به بعد ایموجی معمولی خودکار با پرمیومش ادیت میشه.
`.pe list` `.pe del 😀` `.pe clear` `.pe on|off`
⚠️ اکانت باید تلگرام پریمیوم داشته باشه."""

_pe_cache: dict[int, dict] = {}


# ---------------- helpers ----------------
async def status_text(uid) -> str:
    on = "🟢" if uid in clients else "🔴"
    bal = db.fmt(uid, await db.balance(uid))
    return (f"**📊 وضعیت سلف**\n{on} سلف: {'فعال' if uid in clients else 'غیرفعال'}\n"
            f"💎 الماس شما: {bal}\n🕒 {hm_at(time.time())}")


async def get_map(uid) -> dict:
    if uid not in _pe_cache:
        _pe_cache[uid] = await db.pe_all(uid)
    return _pe_cache[uid]


def u16slice(text: str, off: int, ln: int) -> str:
    b = text.encode("utf-16-le")
    return b[off * 2:(off + ln) * 2].decode("utf-16-le", "ignore")


def strip_ranges(text: str, ents) -> str:
    b = bytearray(text.encode("utf-16-le"))
    for en in sorted(ents, key=lambda x: -x.offset):
        del b[en.offset * 2:(en.offset + en.length) * 2]
    return b.decode("utf-16-le", "ignore")


def custom_ents(msg):
    return [en for en in (getattr(msg, "entities", None) or []) if isinstance(en, MessageEntityCustomEmoji)]


# ---------------- live clock ----------------
async def _read_profile(c):
    me = await c.get_me()
    full = await c(functions.users.GetFullUserRequest(types.InputUserSelf()))
    return me.first_name or "", full.full_user.about or ""


async def _base(c, uid):
    """Name/bio without our clock suffix (kept in DB so we always restore the real ones)."""
    name, about = await _read_profile(c)
    bn, ba = await db.uget(uid, "base_name"), await db.uget(uid, "base_about")
    if bn is None or not name.startswith(bn):
        bn = name
        await db.uset(uid, "base_name", bn)
    if ba is None or not about.startswith(ba):
        ba = about
        await db.uset(uid, "base_about", ba)
    return bn, ba


async def apply_clock(c, uid, ts: float):
    loc = await db.uget(uid, "clock_loc", "name")
    font = int(await db.uget(uid, "clock_font", "1"))
    t = style_time(hm_at(ts), font)
    bn, ba = await _base(c, uid)
    kw = {}
    if loc in ("name", "both"):
        kw["first_name"] = f"{bn} | {t}"[:64]
    if loc in ("bio", "both"):
        kw["about"] = (f"{ba} | {t}" if ba else t)[:70]
    if kw:
        await c(functions.account.UpdateProfileRequest(**kw))


async def restore_profile(c, uid, which=("name", "bio")):
    kw = {}
    if "name" in which and await db.uget(uid, "base_name") is not None:
        kw["first_name"] = await db.uget(uid, "base_name")
    if "bio" in which and await db.uget(uid, "base_about") is not None:
        kw["about"] = await db.uget(uid, "base_about")
    if kw:
        await c(functions.account.UpdateProfileRequest(**kw))


async def set_clock(uid, on: bool):
    c = clients.get(uid)
    await db.uset_flag(uid, "clock", on)
    if not c:
        return
    if on:
        await _base(c, uid)
        await apply_clock(c, uid, time.time())
    else:
        await restore_profile(c, uid)


async def set_loc(uid, loc: str):
    old = await db.uget(uid, "clock_loc", "name")
    await db.uset(uid, "clock_loc", loc)
    c = clients.get(uid)
    if c and await db.uflag(uid, "clock"):
        gone = [w for w in ("name", "bio") if (old in (w, "both")) and loc not in (w, "both")]
        if gone:
            await restore_profile(c, uid, tuple(gone))
        await apply_clock(c, uid, time.time())


async def set_font(uid, font: int):
    await db.uset(uid, "clock_font", font % len(FONTS))
    c = clients.get(uid)
    if c and await db.uflag(uid, "clock"):
        await apply_clock(c, uid, time.time())


async def cycle_font(uid):
    await set_font(uid, int(await db.uget(uid, "clock_font", "1")) + 1)


async def cycle_loc(uid):
    cur = await db.uget(uid, "clock_loc", "name")
    await set_loc(uid, LOCS[(LOCS.index(cur) + 1) % len(LOCS)] if cur in LOCS else "name")


async def clock_loop(c, uid):
    """Fires slightly BEFORE each minute boundary (compensating API latency) so the new minute
    is already on the profile when the minute changes."""
    lat = 0.35
    last_target = 0.0
    while True:
        try:
            now = time.time()
            nxt = max((int(now // 60) + 1) * 60, last_target + 60)
            await asyncio.sleep(max(nxt - lat - time.time(), 0))
            last_target = nxt
            if not await db.uflag(uid, "clock"):
                continue
            t0 = time.time()
            await apply_clock(c, uid, nxt)
            lat = min(max(0.6 * lat + 0.4 * (time.time() - t0), 0.05), 3.0)
        except asyncio.CancelledError:
            raise
        except errors.FloodWaitError as ex:
            log.warning("clock flood %s: %ss", uid, ex.seconds)
            await asyncio.sleep(ex.seconds + 1)
        except errors.rpcerrorlist.FirstNameInvalidError:
            await asyncio.sleep(30)
        except Exception as ex:
            if "not modified" not in str(ex).lower() and "NAME_NOT_MODIFIED" not in str(ex):
                log.warning("clock %s: %s", uid, ex)
            await asyncio.sleep(1)


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


async def c_clock(c, e, uid, arg):
    a = arg.lower().strip()
    if a in ("on", "off"):
        await set_clock(uid, a == "on")
        return await e.edit("🕒 ساعت زنده روشن شد" if a == "on" else "🕒 ساعت خاموش شد و اسم/بیو برگشت")
    if a in LOCS:
        await set_loc(uid, a)
        return await e.edit(f"📍 ساعت حالا توی «{LOC_LABEL[a]}» نمایش داده میشه")
    await e.edit("`.clock on|off` یا `.clock name|bio|both`")


async def c_clockfont(c, e, uid, arg):
    a = arg.strip()
    if a.isdigit() and 1 <= int(a) <= len(FONTS):
        await set_font(uid, int(a) - 1)
        return await e.edit(f"🔤 فونت {a} انتخاب شد: {style_time('12:34', int(a) - 1)}")
    lines = "\n".join(f"{i}. {name}: {style_time('12:34', i - 1)}" for i, (name, _) in enumerate(FONTS, 1))
    await e.edit(f"🔤 **فونت‌های ساعت**\n{lines}\n\nمثال: `.clockfont 3`")


async def c_pe(c, e, uid, arg):
    sub, _, rest = arg.partition(" ")
    sub, rest = sub.lower(), rest.strip()
    m = await get_map(uid)

    if sub in ("", "list"):
        keys = " ".join(m.keys()) or "—"
        return await e.edit(f"✨ ایموجی‌های ثبت‌شده ({len(m)}):\n{keys}\n\nثبت: `.pe add` + ایموجی پرمیوم")

    if sub in ("on", "off"):
        await db.uset_flag(uid, "pemoji", sub == "on")
        return await e.edit("✨ ایموجی پرمیوم روشن شد" if sub == "on" else "✨ ایموجی پرمیوم خاموش شد")

    if sub == "clear":
        await db.pe_clear(uid)
        m.clear()
        return await e.edit("🗑 همه‌ی ایموجی‌های ثبت‌شده پاک شد")

    if sub == "del":
        key = rest.replace(FE0F, "").strip()
        if not key or key not in m:
            return await e.edit("این ایموجی ثبت نشده. مثال: `.pe del 😀`")
        await db.pe_del(uid, key)
        m.pop(key, None)
        return await e.edit(f"🗑 {key} حذف شد")

    if sub == "add":
        msg = e.message
        ents = custom_ents(msg)
        source, explicit = msg.message or "", None
        if ents:
            rem = strip_ranges(source, ents)
            rem = re.sub(r"^\s*\.pe\s+add", "", rem, flags=re.I).strip()
            if rem:
                if len(ents) != 1:
                    return await e.edit("برای ثبت دستی فقط یک ایموجی پرمیوم کنار ایموجی معمولی بذار")
                explicit = rem
        elif e.is_reply:
            r = await e.get_reply_message()
            ents = custom_ents(r) if r else []
            source = (r.message or "") if r else ""
        if not ents:
            return await e.edit(
                "ایموجی پرمیومی پیدا نشد 🤔\n"
                "همراه `.pe add` ایموجی پرمیوم بفرست، یا روی پیامی که داره ریپلای کن.")
        done = []
        for en in ents:
            key = (explicit or u16slice(source, en.offset, en.length)).replace(FE0F, "").strip()
            if not key or len(key) > 12:
                continue
            await db.pe_set(uid, key, en.document_id)
            m[key] = en.document_id
            done.append(key)
        if not done:
            return await e.edit("ثبت نشد؛ ایموجی پایه‌ی معتبر پیدا نکردم.")
        return await e.edit(f"✅ {len(done)} ایموجی ثبت شد:\n{' '.join(done)}\n\nاز این به بعد خودکار جایگزین می‌شن ✨")

    await e.edit("دستور نامعتبر. `.help` بزن.")


COMMANDS = {"panel": c_panel, "help": c_help, "status": c_status, "clock": c_clock,
            "clockfont": c_clockfont, "pe": c_pe}


async def apply_premium(c, e, uid):
    msg = e.message
    text = msg.message or ""
    if not text or text.startswith("."):
        return
    m = await get_map(uid)
    if not m or not await db.uflag(uid, "pemoji", True):
        return
    ents = list(msg.entities or [])
    occupied = [(en.offset, en.offset + en.length) for en in custom_ents(msg)]
    keys = sorted(m, key=len, reverse=True)
    out = []
    i, off, n = 0, 0, len(text)
    while i < n:
        matched = None
        for k in keys:
            if text.startswith(k + FE0F, i):
                matched = (k + FE0F, k)
                break
            if text.startswith(k, i):
                matched = (k, k)
                break
        if matched:
            cand, k = matched
            ln = len(cand.encode("utf-16-le")) // 2
            nxt = text[i + len(cand):i + len(cand) + 1]
            glued = bool(nxt) and (nxt == "‍" or "\U0001F3FB" <= nxt <= "\U0001F3FF")
            if not glued and not any(a <= off < b for a, b in occupied):
                out.append(MessageEntityCustomEmoji(offset=off, length=ln, document_id=m[k]))
                i += len(cand)
                off += ln
                continue
        off += 2 if ord(text[i]) > 0xFFFF else 1
        i += 1
    if not out:
        return
    allents = sorted(ents + out, key=lambda x: x.offset)
    try:
        await e.edit(text, formatting_entities=allents)
    except Exception as ex:
        log.warning("premium emoji edit failed for %s: %s", uid, ex)


def attach(c: TelegramClient, uid: int):
    @c.on(events.NewMessage(outgoing=True))
    async def on_out(e):
        text = e.message.message or ""
        m = re.match(r"^\.(\S+)\s*(.*)$", text, re.S)
        fn = COMMANDS.get(m.group(1).lower()) if m else None
        if not fn:
            return await apply_premium(c, e, uid)
        try:
            await fn(c, e, uid, m.group(2).strip())
        except Exception as ex:
            log.exception("command %s failed", m.group(1))
            try:
                await e.edit(f"⚠️ خطا: {type(ex).__name__}")
            except Exception:
                pass


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
    _tasks[uid] = asyncio.create_task(clock_loop(c, uid))
    log.info("self started for %s", uid)
    return True


async def stop_user(uid: int):
    _pe_cache.pop(uid, None)
    t = _tasks.pop(uid, None)
    if t:
        t.cancel()
    c = clients.pop(uid, None)
    if c:
        try:
            if await db.uflag(uid, "clock"):
                await restore_profile(c, uid)
        except Exception:
            pass
        try:
            await c.disconnect()
        except Exception:
            pass
