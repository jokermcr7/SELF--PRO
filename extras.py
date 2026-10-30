"""Self extras (v1.8): group guard, AI secretary, giveaway + poll, daily messages, live profile stats.

selfbot.py calls install() once (adds the commands) and, per user, hooks: on_out / on_in / loops.
"""
import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from telethon import errors, functions, types

import db
import utils

log = logging.getLogger("extras")
TZ = ZoneInfo("Asia/Tehran")
Z = "‌"


def n_(w):
    return re.sub(Z, "", (w or "").replace("ي", "ی").replace("ك", "ک")).lower()


def en(s):
    return utils.to_en_digits(s or "")


def onoff(w):
    a = n_(w)
    if a in ("روشن", "on", "1", "فعال"):
        return True
    if a in ("خاموش", "off", "0", "غیرفعال"):
        return False
    return None


async def jget(uid, key, default):
    v = await db.uget(uid, key)
    if v is None:
        return default
    try:
        return json.loads(v)
    except ValueError:
        return default


async def jset(uid, key, val):
    await db.uset(uid, key, json.dumps(val, ensure_ascii=False))


def _safe_task(coro):
    t = asyncio.create_task(coro)
    t.add_done_callback(lambda x: x.exception() and log.warning("task failed: %s", x.exception()))
    return t


async def _later_delete(msg, secs=8):
    await asyncio.sleep(secs)
    try:
        await msg.delete()
    except Exception:                                                  # noqa: BLE001
        pass


# ====================================================================== 1) group guard (anti spam / link)
LINK_RE = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/|telegram\.dog/|tg://|\b[a-z0-9-]{2,}\.(?:com|ir|org|net|me|io|xyz|ru|info|site|link|shop|top|cc|ly|gl)\b)", re.I)
GUARD_DEFAULT = {"on": False, "link": True, "fwd": True, "rep": True, "warns": 3, "white": []}
_cfg_cache: dict = {}
_adm_cache: dict = {}
_hist: dict = {}
_warns: dict = {}
_strikes: dict = {}
_noadmin_told: set = set()


async def guard_cfg(uid, chat):
    k = (uid, chat)
    v = _cfg_cache.get(k)
    if v and time.time() - v[0] < 30:
        return v[1]
    cfg = {**GUARD_DEFAULT, **(await jget(uid, f"ag:{chat}", {}))}
    _cfg_cache[k] = (time.time(), cfg)
    return cfg


async def guard_save(uid, chat, cfg):
    await jset(uid, f"ag:{chat}", cfg)
    _cfg_cache[(uid, chat)] = (time.time(), cfg)


async def is_admin(c, chat, user):
    k = (chat, user)
    v = _adm_cache.get(k)
    if v and time.time() - v[0] < 300:
        return v[1]
    try:
        p = await c.get_permissions(chat, user)
        r = bool(p.is_admin or p.is_creator)
    except Exception:                                                  # noqa: BLE001
        r = False
    _adm_cache[k] = (time.time(), r)
    return r


def _violation(e, cfg):
    text = e.raw_text or ""
    if cfg["link"]:
        ents = e.message.entities or []
        if LINK_RE.search(text) or any(isinstance(x, (types.MessageEntityUrl, types.MessageEntityTextUrl)) for x in ents):
            return "ارسال لینک"
    if cfg["fwd"] and e.message.fwd_from:
        return "فوروارد"
    if cfg["rep"]:
        k = (e.chat_id, e.sender_id)
        now = time.time()
        h = [x for x in _hist.get(k, []) if now - x[0] < 10] + [(now, text.strip().lower())]
        _hist[k] = h
        if len(h) >= 6 and now - h[-6][0] < 8:
            return "اسپم (پیام پشت‌سرهم)"
        if text.strip() and sum(1 for x in h if x[1] == h[-1][1]) >= 3:
            return "تکرار پیام"
    return None


async def guard(c, e, uid):
    """Group message from someone else. Returns True when the message was removed."""
    if not e.sender_id or e.sender_id == uid:
        return False
    cfg = await guard_cfg(uid, e.chat_id)
    if not cfg["on"] or e.sender_id in cfg["white"]:
        return False
    if await is_admin(c, e.chat_id, e.sender_id):
        return False
    why = _violation(e, cfg)
    if not why:
        return False
    try:
        await c.delete_messages(e.chat_id, [e.id], revoke=True)
    except Exception:                                                  # noqa: BLE001
        if e.chat_id not in _noadmin_told:
            _noadmin_told.add(e.chat_id)
            try:
                await c.send_message("me", "⚠️ ضد اسپم نتونست پیام رو حذف کنه؛ توی اون گروه باید ادمین با دسترسی «حذف پیام» باشی.")
            except Exception:                                          # noqa: BLE001
                pass
        return False
    k = (e.chat_id, e.sender_id)
    _warns[k] = _warns.get(k, 0) + 1
    try:
        sender = await e.get_sender()
        nm = utils.clean_name(getattr(sender, "first_name", "") or str(e.sender_id))
    except Exception:                                                  # noqa: BLE001
        nm = str(e.sender_id)
    note = f"⚠️ [{nm}](tg://user?id={e.sender_id}) — {why} ممنوعه! اخطار {_warns[k]} از {cfg['warns']}"
    if _warns[k] >= cfg["warns"]:
        _warns[k] = 0
        _strikes[k] = _strikes.get(k, 0) + 1
        try:
            if _strikes[k] == 1:
                await c.edit_permissions(e.chat_id, e.sender_id, until_date=datetime.now(TZ) + timedelta(hours=1), send_messages=False)
                note = f"🔇 [{nm}](tg://user?id={e.sender_id}) به‌خاطر تکرار تخلف ({why}) ۱ ساعت سکوت شد."
            else:
                await c.edit_permissions(e.chat_id, e.sender_id, view_messages=False)
                note = f"⛔ [{nm}](tg://user?id={e.sender_id}) به‌خاطر تکرار تخلف از گروه بن شد."
        except Exception:                                              # noqa: BLE001
            note += "\n(برای سکوت/بن باید ادمین با دسترسی محدود کردن باشم)"
    try:
        m = await c.send_message(e.chat_id, note)
        _safe_task(_later_delete(m, 10))
    except Exception:                                                  # noqa: BLE001
        pass
    return True


GUARD_HELP = ("🛡 **ضد اسپم و ضد لینک گروه** (داخل خود گروه بزن؛ باید ادمین باشی)\n"
              "`.ضداسپم روشن` / `.ضداسپم خاموش`\n"
              "`.ضداسپم لینک روشن|خاموش` — حذف لینک\n`.ضداسپم فوروارد روشن|خاموش`\n"
              "`.ضداسپم تکرار روشن|خاموش` — پیام تکراری و پشت‌سرهم\n"
              "`.ضداسپم اخطار 3` — تعداد اخطار تا سکوت ۱ ساعته (بار بعد: بن)\n"
              "`.ضداسپم سفید` (ریپلای) — معاف کردن یه نفر | `.ضداسپم سفید حذف` (ریپلای)\n"
              "`.ضداسپم وضعیت`")


async def c_guard(c, e, uid, arg):
    if e.is_private:
        return await e.edit(GUARD_HELP)
    cfg = await guard_cfg(uid, e.chat_id)
    w = arg.split()
    sub = n_(w[0]) if w else ""
    labels = {"لینک": "link", "فوروارد": "fwd", "تکرار": "rep", "اسپم": "rep"}
    if sub in ("روشن", "خاموش", "on", "off"):
        cfg["on"] = onoff(sub)
        await guard_save(uid, e.chat_id, cfg)
        return await e.edit("🟢 ضد اسپم این گروه روشن شد" if cfg["on"] else "🔴 ضد اسپم این گروه خاموش شد")
    if sub in labels and len(w) > 1 and onoff(w[1]) is not None:
        cfg[labels[sub]] = onoff(w[1])
        await guard_save(uid, e.chat_id, cfg)
        return await e.edit(f"{'🟢' if cfg[labels[sub]] else '🔴'} {sub}: {'روشن' if cfg[labels[sub]] else 'خاموش'}")
    if sub == "اخطار" and len(w) > 1 and en(w[1]).isdigit() and 1 <= int(en(w[1])) <= 10:
        cfg["warns"] = int(en(w[1]))
        await guard_save(uid, e.chat_id, cfg)
        return await e.edit(f"⚠️ بعد از {cfg['warns']} اخطار: سکوت ۱ ساعته")
    if sub in ("سفید", "white"):
        r = await e.get_reply_message() if e.is_reply else None
        if not r or not r.sender_id:
            return await e.edit("روی پیام کاربر ریپلای کن: `.ضداسپم سفید`")
        if len(w) > 1 and n_(w[1]) in ("حذف", "del"):
            cfg["white"] = [x for x in cfg["white"] if x != r.sender_id]
            msg = "❌ از لیست سفید حذف شد"
        else:
            cfg["white"] = list({*cfg["white"], r.sender_id})
            msg = "✅ به لیست سفید اضافه شد (ضد اسپم روش اعمال نمیشه)"
        await guard_save(uid, e.chat_id, cfg)
        return await e.edit(msg)
    if sub in ("وضعیت", "status"):
        f = lambda v: "🟢" if v else "🔴"                              # noqa: E731
        return await e.edit(f"🛡 **ضد اسپم این گروه** {f(cfg['on'])}\nلینک {f(cfg['link'])} | فوروارد {f(cfg['fwd'])} | تکرار {f(cfg['rep'])}\n"
                            f"اخطار تا سکوت: {cfg['warns']} | لیست سفید: {len(cfg['white'])} نفر")
    await e.edit(GUARD_HELP)


# ====================================================================== 2) AI secretary
AI_KEY = os.getenv("AI_API_KEY", "")
AI_BASE = os.getenv("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")
_ai_ctx: dict = {}
_ai_seen: dict = {}


async def ai_complete(messages):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as s:
        async with s.post(f"{AI_BASE}/chat/completions", headers={"Authorization": f"Bearer {AI_KEY}"},
                          json={"model": AI_MODEL, "messages": messages, "max_tokens": 220, "temperature": 0.7}) as r:
            data = await r.json(content_type=None)
    return data["choices"][0]["message"]["content"].strip()


async def ai_reply(c, e, uid, name):
    """Private message while the AI secretary is on. Returns True when a reply was sent."""
    if not AI_KEY or not await db.uflag(uid, "ai", False):
        return False
    text = (e.raw_text or "").strip()
    if not text:
        return False
    k = (uid, e.sender_id)
    now = time.time()
    hist = [t for t in _ai_seen.get(k, []) if now - t < 3600]
    if (hist and now - hist[-1] < 12) or len(hist) >= 12:
        return False
    _ai_seen[k] = hist + [now]
    owner = await db.uget(uid, "ai_owner", "صاحب این اکانت")
    tone = await db.uget(uid, "ai_tone", "دوستانه و کوتاه")
    rules = await db.uget(uid, "ai_rules", "")
    system = (f"تو دستیار هوشمند (منشی خودکار) {owner} هستی و وقتی خودش در دسترس نیست به پیام‌ها جواب میدی. "
              f"فارسی و با لحن {tone} و حداکثر ۲ جمله جواب بده. وانمود نکن انسان یا خود {owner} هستی؛ اگه پرسیدن بگو دستیار هوشمندی. "
              "قول، تعهد، پرداخت یا اطلاعات شخصی نده؛ اگه موضوع مهمی بود بگو پیام رو بهش میرسونی. " + rules)
    ctx = _ai_ctx.setdefault(k, [])
    ctx.append({"role": "user", "content": text[:800]})
    del ctx[:-6]
    try:
        reply = await ai_complete([{"role": "system", "content": system}] + ctx)
    except Exception as ex:                                            # noqa: BLE001
        log.warning("ai reply failed: %s", ex)
        return False
    ctx.append({"role": "assistant", "content": reply})
    await e.reply("🤖 " + reply[:900])
    return True


async def c_ai(c, e, uid, arg):
    w = arg.split()
    sub = n_(w[0]) if w else ""
    if sub in ("روشن", "خاموش"):
        if sub == "روشن" and not AI_KEY:
            return await e.edit("⚠️ کلید هوش مصنوعی تنظیم نشده. مدیر ربات باید `AI_API_KEY` (و در صورت نیاز `AI_BASE_URL` و `AI_MODEL`) رو توی Railway بذاره.")
        await db.uset_flag(uid, "ai", sub == "روشن")
        return await e.edit("🟢 منشی هوشمند روشن شد؛ به پیوی‌ها جواب هوشمند میده" if sub == "روشن" else "🔴 منشی هوشمند خاموش شد")
    if sub == "لحن" and len(w) > 1:
        await db.uset(uid, "ai_tone", " ".join(w[1:])[:60])
        return await e.edit(f"🎭 لحن: {' '.join(w[1:])[:60]}")
    if sub == "اسم" and len(w) > 1:
        await db.uset(uid, "ai_owner", " ".join(w[1:])[:40])
        return await e.edit(f"✅ دستیار خودش رو دستیار «{' '.join(w[1:])[:40]}» معرفی می‌کنه")
    if sub == "قانون" and len(w) > 1:
        await db.uset(uid, "ai_rules", " ".join(w[1:])[:300])
        return await e.edit("📜 قانون اضافه ذخیره شد")
    on = await db.uflag(uid, "ai", False)
    await e.edit(f"🤖 **منشی هوشمند** {'🟢 روشن' if on else '🔴 خاموش'}{'' if AI_KEY else ' (کلید AI_API_KEY تنظیم نشده)'}\n"
                 "به پیوی‌ها (هر نفر حداکثر ۱۲ جواب در ساعت) جواب هوشمند و کوتاه میده.\n"
                 "`.هوش_منشی روشن` / `.هوش_منشی خاموش`\n`.هوش_منشی لحن رسمی و مودب`\n"
                 "`.هوش_منشی اسم علی` — اسمی که دستیار ازت میبره\n`.هوش_منشی قانون درباره‌ی قیمت‌ها چیزی نگو`")


# ====================================================================== 3) giveaway + poll
_gives: dict = {}                      # chat -> state


async def _rnd_ints(n, lo, hi):
    import groupgames
    return await groupgames.rnd_ints(n, lo, hi)


async def c_giveaway(c, e, uid, arg):
    """.قرعه_کشی دقیقه | تعداد برنده | الماس هر برنده | عنوان جایزه"""
    a = arg.strip()
    if n_(a) in ("لغو", "cancel"):
        g = _gives.pop(e.chat_id, None)
        if g:
            g["task"].cancel()
        return await e.edit("❌ قرعه‌کشی لغو شد" if g else "قرعه‌کشی فعالی نیست")
    p = [x.strip() for x in a.split("|")]
    if len(p) < 2 or not all(en(x).isdigit() for x in p[:2]):
        return await e.edit("🎁 **قرعه‌کشی**\n`.قرعه_کشی 10 | 2 | 50 | کارت هدیه`\n(مدت به دقیقه | تعداد برنده | الماس هر برنده (اختیاری) | عنوان جایزه)\n"
                            "اعضا با نوشتن «شرکت» ثبت‌نام می‌کنن؛ برنده‌ها با random.org انتخاب میشن.\n`.قرعه_کشی لغو`")
    mins, winners = int(en(p[0])), int(en(p[1]))
    diamonds = int(en(p[2])) if len(p) > 2 and en(p[2]).isdigit() else 0
    title = p[3] if len(p) > 3 else ("جایزه‌ی الماس" if diamonds else "جایزه")
    if not (1 <= mins <= 1440 and 1 <= winners <= 20) or diamonds > 10 ** 7:
        return await e.edit("مدت ۱ تا ۱۴۴۰ دقیقه و برنده‌ها ۱ تا ۲۰ نفر")
    if e.chat_id in _gives:
        return await e.edit("توی این چت یه قرعه‌کشی فعال داری؛ `.قرعه_کشی لغو`")
    if diamonds and uid not in db.ADMINS and await db.balance(uid) < diamonds * winners:
        return await e.edit(f"💎 برای پرداخت جایزه‌ها {diamonds * winners:,} الماس لازمه؛ موجودی کافی نیست")
    end = time.time() + mins * 60
    g = {"people": {}, "winners": winners, "diamonds": diamonds, "title": title, "end": end}
    _gives[e.chat_id] = g
    prize = f"{title}" + (f" + {diamonds:,} 💎 برای هر برنده" if diamonds else "")
    await e.edit(f"🎁 **قرعه‌کشی شروع شد!**\n🏆 جایزه: **{prize}**\n👥 تعداد برنده: **{winners}**\n"
                 f"⏳ مهلت: **{mins} دقیقه**\n\n👉 برای شرکت بنویس: **شرکت**\n🍀 برنده‌ها با random.org انتخاب میشن")

    async def finish():
        await asyncio.sleep(mins * 60)
        _gives.pop(e.chat_id, None)
        pool = list(g["people"].items())
        if not pool:
            return await c.send_message(e.chat_id, "😕 کسی توی قرعه‌کشی شرکت نکرد.")
        lines, paid = [], 0
        for i in range(min(winners, len(pool))):
            idx = (await _rnd_ints(1, 0, len(pool) - 1))[0]
            wid, nm = pool.pop(idx)
            line = f"{i + 1}. [{nm}](tg://user?id={wid})"
            if diamonds:
                if await db.transfer(uid, wid, diamonds, nm) is not None:
                    line += f" — {diamonds:,} 💎 ✅"
                    paid += 1
                else:
                    line += " — (پرداخت الماس انجام نشد)"
            lines.append(line)
        await c.send_message(e.chat_id, f"🎉 **نتیجه‌ی قرعه‌کشی** ({len(g['people'])} شرکت‌کننده)\n🏆 {prize}\n\n" + "\n".join(lines)
                             + "\n\n🍀 قرعه: random.org")

    g["task"] = asyncio.create_task(finish())


async def giveaway_hook(c, e, uid):
    g = _gives.get(e.chat_id)
    if not g or not e.sender_id or e.sender_id == uid or time.time() > g["end"]:
        return
    if n_((e.raw_text or "").strip()) in ("شرکت", "من", "join", "شرکت میکنم", "شرکت می‌کنم"):
        if e.sender_id not in g["people"]:
            try:
                s = await e.get_sender()
                g["people"][e.sender_id] = utils.clean_name(getattr(s, "first_name", "") or str(e.sender_id))
            except Exception:                                          # noqa: BLE001
                g["people"][e.sender_id] = str(e.sender_id)
            try:
                await c.send_read_acknowledge(e.chat_id, e.message)
            except Exception:                                          # noqa: BLE001
                pass


async def c_poll(c, e, uid, arg):
    p = [x.strip() for x in arg.split("|") if x.strip()]
    if len(p) < 3 or len(p) > 11:
        return await e.edit("📊 **نظرسنجی**\n`.نظرسنجی کدوم بهتره؟ | گزینه‌ی اول | گزینه‌ی دوم | گزینه‌ی سوم`\n(۲ تا ۱۰ گزینه)")
    q, opts = p[0], p[1:]
    try:
        try:
            poll = types.Poll(id=random.getrandbits(62), question=types.TextWithEntities(q, []),
                              answers=[types.PollAnswer(text=types.TextWithEntities(o, []), option=bytes([i])) for i, o in enumerate(opts)])
        except Exception:                                              # noqa: BLE001   (older layer: plain strings)
            poll = types.Poll(id=random.getrandbits(62), question=q,
                              answers=[types.PollAnswer(text=o, option=bytes([i])) for i, o in enumerate(opts)])
        await c.send_message(e.chat_id, file=types.InputMediaPoll(poll=poll))
        await e.delete()
    except Exception as ex:                                            # noqa: BLE001
        log.warning("poll failed: %s", ex)
        await e.edit(f"⚠️ ساخت نظرسنجی ناموفق بود ({type(ex).__name__})")


# ====================================================================== 4) daily messages
QUOTES = ["هر روز یه شروع تازه‌ست؛ ازش درست استفاده کن ☀️", "موفقیت یعنی هر روز یه قدم کوچیک رو به جلو 🚀",
          "امروز بهترین نسخه‌ی خودت باش 💪", "آروم باش، ادامه بده؛ دیر نیست ⏳", "کار امروز رو به فردا ننداز ✅",
          "لبخند بزن؛ دنیا قشنگ‌تر میشه 😊", "سختی‌ها موقتن، تلاشت ماندگاره 🌱", "به خودت ایمان داشته باش، تو می‌تونی 🔥",
          "یه روز عالی داشته باشی! 🌟", "اولین قدم سخت‌ترینه؛ برش دار 👣", "هر چی بکاری همون رو درو می‌کنی 🌾",
          "صبر کن؛ بهترین‌ها دیر میان ✨", "قدر لحظه‌هات رو بدون 🕊", "امروز یه کار خوب انجام بده 🤝", "آرامش بهترین سرمایه‌ست 🌿"]


async def c_daily(c, e, uid, arg):
    items = await jget(uid, "daily", [])
    w = arg.split()
    sub = n_(w[0]) if w else ""
    if sub in ("لیست", "list"):
        if not items:
            return await e.edit("هنوز پیام روزانه‌ای نداری")
        return await e.edit("🗓 **پیام‌های روزانه**\n" + "\n".join(
            f"{i + 1}. ⏰ {x['at']} | چت {x['chat']} | {x['text'][:40]}" for i, x in enumerate(items)))
    if sub in ("حذف", "del") and len(w) > 1 and en(w[1]).isdigit() and 1 <= int(en(w[1])) <= len(items):
        x = items.pop(int(en(w[1])) - 1)
        await jset(uid, "daily", items)
        return await e.edit(f"❌ حذف شد: {x['text'][:40]}")
    m = re.match(r"^(?:ساعت\s*)?([0-2]?\d[:٫.][0-5]\d)\s*[|:]?\s*(.*)$", en(arg), re.S) if arg else None
    if not m:
        return await e.edit("🗓 **پیام روزانه** (هر روز سر ساعت توی همین چت ارسال میشه)\n"
                            "`.پیام_روزانه 08:30 | صبح بخیر دوستان ☀️`\n`.پیام_روزانه 21:00 | انگیزشی` — هر روز یه جمله‌ی انگیزشی\n"
                            "`.پیام_روزانه لیست` | `.پیام_روزانه حذف 1`")
    hh, mm = re.split(r"[:٫.]", m.group(1))
    at = f"{int(hh):02d}:{mm}"
    if int(hh) > 23:
        return await e.edit("ساعت نامعتبره")
    text = m.group(2).strip().lstrip("|").strip()
    if not text:
        return await e.edit("متن پیام رو هم بنویس")
    if len(items) >= 10:
        return await e.edit("حداکثر ۱۰ پیام روزانه")
    items.append({"chat": e.chat_id, "at": at, "text": text, "last": ""})
    await jset(uid, "daily", items)
    await e.edit(f"✅ هر روز ساعت **{at}** توی همین چت ارسال میشه:\n{text[:100] if n_(text) != 'انگیزشی' else '(جمله‌ی انگیزشی تصادفی)'}")


async def daily_loop(c, uid):
    while True:
        try:
            await asyncio.sleep(20)
            if not await db.user_is_active(uid) or not await db.uflag(uid, "selfon", True):
                continue
            items = await jget(uid, "daily", [])
            if not items:
                continue
            now = datetime.now(TZ)
            hm, day = now.strftime("%H:%M"), now.date().isoformat()
            changed = False
            for x in items:
                if x["at"] == hm and x.get("last") != day:
                    x["last"] = day
                    changed = True
                    txt = random.choice(QUOTES) if n_(x["text"]) == "انگیزشی" else x["text"]
                    try:
                        await c.send_message(x["chat"], txt)
                    except Exception as ex:                            # noqa: BLE001
                        log.warning("daily send failed: %s", ex)
            if changed:
                await jset(uid, "daily", items)
        except asyncio.CancelledError:
            raise
        except Exception as ex:                                        # noqa: BLE001
            log.warning("daily loop %s: %s", uid, ex)
            await asyncio.sleep(10)


# ====================================================================== 5) live profile stats (bio)
_stats: dict = {}
STAT_EVERY = 600


def _st(uid):
    today = datetime.now(TZ).date().isoformat()
    s = _stats.get(uid)
    if not s or s["day"] != today:
        s = _stats[uid] = {"day": today, "sent": 0, "recv": 0, "last": 0.0}
    return s


def count_out(uid):
    s = _st(uid)
    s["sent"] += 1
    s["last"] = time.time()


def count_in(uid):
    _st(uid)["recv"] += 1


DEFAULT_TPL = "{online} | امروز {sent} پیام"


def render_stats(uid, tpl):
    s = _st(uid)
    online = "آنلاین ✅" if time.time() - s["last"] < 300 else "آفلاین 💤"
    return (tpl.replace("{online}", online).replace("{sent}", str(s["sent"])).replace("{recv}", str(s["recv"]))
            .replace("{date}", datetime.now(TZ).strftime("%m/%d")))[:70]


async def c_stats_bio(c, e, uid, arg):
    w = arg.split()
    sub = n_(w[0]) if w else ""
    if sub in ("روشن", "خاموش"):
        if sub == "روشن" and await db.uflag(uid, "clock_bio", False):
            return await e.edit("⚠️ ساعت زنده روی بیو روشنه؛ اول `.ساعت بیو خاموش` رو بزن (هر دو روی بیو می‌نویسن)")
        await db.uset_flag(uid, "statbio", sub == "روشن")
        if sub == "روشن":
            await db.uset(uid, "statbio_base", (await c(functions.users.GetFullUserRequest(types.InputUserSelf()))).full_user.about or "")
            await push_stats(c, uid)
            return await e.edit("🟢 آمار زنده‌ی بیو روشن شد؛ هر ۱۰ دقیقه آپدیت میشه\n" + render_stats(uid, await db.uget(uid, "statbio_tpl", DEFAULT_TPL)))
        await c(functions.account.UpdateProfileRequest(about=(await db.uget(uid, "statbio_base", ""))[:140]))
        return await e.edit("🔴 آمار زنده‌ی بیو خاموش شد و بیوی قبلیت برگشت")
    if sub == "قالب" and len(w) > 1:
        await db.uset(uid, "statbio_tpl", arg.split(None, 1)[1][:70])
        return await e.edit("✅ قالب ذخیره شد: " + render_stats(uid, arg.split(None, 1)[1]))
    on = await db.uflag(uid, "statbio", False)
    await e.edit(f"📈 **آمار زنده‌ی پروفایل** {'🟢 روشن' if on else '🔴 خاموش'}\nبیوی تو هر ۱۰ دقیقه خودکار آپدیت میشه:\n"
                 f"{render_stats(uid, await db.uget(uid, 'statbio_tpl', DEFAULT_TPL))}\n\n`.آمار_پروفایل روشن` / `.آمار_پروفایل خاموش`\n"
                 "`.آمار_پروفایل قالب {online} | {sent} پیام ارسالی | {recv} دریافتی`\nمتغیرها: {online} {sent} {recv} {date} (حداکثر ۷۰ حرف)")


async def push_stats(c, uid):
    _st(uid)
    text = render_stats(uid, await db.uget(uid, "statbio_tpl", DEFAULT_TPL))
    if text != _stats[uid].get("shown"):
        await c(functions.account.UpdateProfileRequest(about=text))
        _stats[uid]["shown"] = text


async def stats_loop(c, uid):
    while True:
        try:
            await asyncio.sleep(STAT_EVERY)
            if not await db.uflag(uid, "statbio", False) or await db.uflag(uid, "clock_bio", False):
                continue
            if not await db.user_is_active(uid) or not await db.uflag(uid, "selfon", True):
                continue
            await push_stats(c, uid)
        except asyncio.CancelledError:
            raise
        except errors.FloodWaitError as ex:
            await asyncio.sleep(min(ex.seconds, 1800) + 1)
        except Exception as ex:                                        # noqa: BLE001
            log.warning("stats loop %s: %s", uid, ex)
            await asyncio.sleep(30)


# ====================================================================== wiring
COMMAND_NAMES = {
    "ضداسپم": ("guard", c_guard), "ضد_اسپم": ("guard", c_guard), "ضدلینک": ("guard", c_guard),
    "هوش_منشی": ("ai", c_ai), "منشی_هوشمند": ("ai", c_ai),
    "قرعه_کشی": ("giveaway", c_giveaway), "قرعه‌کشی": ("giveaway", c_giveaway), "نظرسنجی": ("poll", c_poll),
    "پیام_روزانه": ("daily", c_daily), "آمار_پروفایل": ("statbio", c_stats_bio),
}


def install(COMMANDS, FA, norm):
    for name, (key, fn) in COMMAND_NAMES.items():
        COMMANDS[key] = fn
        FA[name] = key


def loops(c, uid):
    return [daily_loop(c, uid), stats_loop(c, uid)]


async def on_group(c, e, uid):
    """Every incoming group message. True = message was removed (stop processing)."""
    await giveaway_hook(c, e, uid)
    return await guard(c, e, uid)
