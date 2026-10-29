"""Manager bot: onboarding (phone + keypad code login), panel, wallet, custom diamond purchase, games."""
import asyncio
import logging
import os

from telethon import Button, TelegramClient, errors, events
from telethon.sessions import StringSession

import assets
import db
import groupgames
import secure
import selfbot
import utils

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_USERNAME = os.environ["BOT_USERNAME"].lstrip("@")
SUPPORT = os.getenv("SUPPORT_USERNAME", "").lstrip("@")
PAYMENT_INFO = os.getenv("PAYMENT_INFO", "")
BRAND = os.getenv("BRAND", "MMD")
ADMINS = db.UNLIMITED
selfbot.BOT_USERNAME = BOT_USERNAME

bot = TelegramClient(StringSession(), API_ID, API_HASH)
login: dict[int, dict] = {}
buy: dict[int, dict] = {}


def price_text() -> str:
    per = db.PRICE_PER_1500 / 1500
    return (f"💳 **خرید الماس**\n\n"
            f"💎 هر ۱٬۵۰۰ الماس = {db.PRICE_PER_1500:,} تومان (هر الماس ≈ {per:.1f} تومان)\n"
            f"🔑 هر ورود سلف = {db.LOGIN_FEE} الماس\n\n"
            f"**تعداد الماس دلخواهت رو بفرست** (فقط عدد، از {db.MIN_BUY} به بالا) تا مبلغ رو حساب کنم 👇")


WELCOME = f"""👋 **به ربات {BRAND} SELF خوش اومدی!**

با ساخت سلف، ساعت زنده، ایموجی پرمیوم خودکار و پنل مدیریت روی اکانتت فعال میشه. بعد از ورود، داخل هر چتی `.panel` بزن.
🎮 بازی‌های الماسی هم فقط داخل گپ مخصوص بازی هست.

💎 قیمت: {1500:,} الماس = {db.PRICE_PER_1500:,} تومان (هر تعداد دلخواه)
🎁 هدیه‌ی اولین ورود: {db.WELCOME_BONUS} الماس
🔑 هر ورود سلف: {db.LOGIN_FEE} الماس

🔐 نشست اکانتت رمزنگاری‌شده ذخیره میشه و هر لحظه با «خروج و حذف سلف» پاک میشه."""


def main_menu():
    return [
        [Button.inline("📲 ساخت / ورود سلف", b"m:login")],
        [Button.inline("💎 موجودی", b"m:bal"), Button.inline("📊 وضعیت", b"m:status")],
        [Button.inline("💳 خرید الماس", b"m:buy"), Button.inline("📖 راهنما", b"m:help")],
        [Button.inline("🚪 خروج و حذف سلف", b"m:logout")],
    ]


def keypad():
    rows = [[Button.inline(str(n), f"k:{n}".encode()) for n in r] for r in ((1, 2, 3), (4, 5, 6), (7, 8, 9))]
    rows.append([Button.inline("⌫", b"k:b"), Button.inline("0", b"k:0"), Button.inline("✅ تایید", b"k:ok")])
    rows.append([Button.inline("🔄 ارسال مجدد کد", b"k:re"), Button.inline("❌ لغو", b"k:x")])
    return rows


def keypad_text(st):
    n, total = len(st["code"]), st["length"]
    mask = " ".join(["●"] * n + ["○"] * max(total - n, 0))
    return (f"🔐 **کد ورود** رو با دکمه‌ها بزن:\n\n`{mask}`\n\n"
            "کد از طرف «Telegram» داخل خود تلگرامت (یا SMS) میاد.")


# =========================================================== login flow
async def cancel_login(uid):
    st = login.pop(uid, None)
    if st:
        if st.get("task"):
            st["task"].cancel()
        if st.get("client"):
            try:
                await st["client"].disconnect()
            except Exception:
                pass


async def login_timeout(uid, st):
    await asyncio.sleep(600)
    if login.get(uid) is st:
        await cancel_login(uid)
        try:
            await bot.send_message(uid, "⌛ زمان ورود تموم شد. دوباره امتحان کن.", buttons=main_menu())
        except Exception:
            pass


async def send_code(uid, st):
    sent = await st["client"].send_code_request(st["phone"])
    st["hash"] = sent.phone_code_hash
    st["length"] = getattr(sent.type, "length", None) or 5
    st["code"] = ""
    st["step"] = "code"


async def complete_login(uid):
    st = login.pop(uid)
    if st.get("task"):
        st["task"].cancel()
    c = st["client"]
    me = await c.get_me()
    if me.id != uid:
        await c.log_out()
        return await bot.send_message(
            uid, "⛔ این شماره متعلق به همین اکانت تلگرامی که با ربات حرف می‌زنی نیست. "
                 "فقط روی اکانت خودت می‌تونی سلف بسازی.", buttons=main_menu())
    # every login costs diamonds; no diamonds => the self does not enter the account
    if await db.try_spend(uid, db.LOGIN_FEE, "self_login") is None:
        await c.log_out()
        return await bot.send_message(
            uid, f"💎 الماس کافی نیست؛ هر ورود سلف {db.LOGIN_FEE} الماس هزینه داره و سلف وارد اکانتت نشد.",
            buttons=main_menu())
    session = c.session.save()
    await c.disconnect()
    await db.user_upsert(uid, st["phone"], secure.enc(session))
    ok = await selfbot.start_user(uid, session, API_ID, API_HASH)
    if not ok:
        await db.add(uid, db.LOGIN_FEE, "self_login_refund")
        return await bot.send_message(uid, "⚠️ ورود انجام شد ولی سلف بالا نیومد؛ الماس برگشت داده شد. دوباره امتحان کن.")
    await bot.send_message(
        uid, f"✅ **سلف فعال شد!** ({db.LOGIN_FEE} 💎 کسر شد)\nداخل هر چتی `.panel` بزن، یا `.help`.\n\n"
             + await selfbot.status_text(uid), buttons=main_menu())


async def submit_code(uid):
    st = login[uid]
    c = st["client"]
    try:
        await c.sign_in(phone=st["phone"], code=st["code"], phone_code_hash=st["hash"])
    except errors.SessionPasswordNeededError:
        st["step"] = "password"
        return await bot.send_message(
            uid, "🔒 رمز تایید دو مرحله‌ای داری. رمزت رو بفرست (پیامت بعد از دریافت پاک میشه).")
    except errors.PhoneCodeInvalidError:
        st["code"] = ""
        return await bot.send_message(uid, "❌ کد اشتباهه. دوباره با دکمه‌ها بزن:", buttons=keypad())
    except errors.PhoneCodeExpiredError:
        st["code"] = ""
        return await bot.send_message(uid, "⌛ کد منقضی شده. «🔄 ارسال مجدد کد» رو بزن:", buttons=keypad())
    except errors.FloodWaitError as ex:
        await cancel_login(uid)
        return await bot.send_message(uid, f"⏳ تلگرام محدودت کرد. {ex.seconds} ثانیه بعد دوباره امتحان کن.",
                                      buttons=main_menu())
    except Exception as ex:
        await cancel_login(uid)
        log.exception("sign_in failed")
        return await bot.send_message(uid, f"⚠️ خطا: {type(ex).__name__}", buttons=main_menu())
    await complete_login(uid)


@bot.on(events.CallbackQuery(pattern=rb"^k:"))
async def cb_key(e):
    uid = e.sender_id
    st = login.get(uid)
    if not st or st["step"] != "code":
        return await e.answer("این مرحله تموم شده. /start بزن", alert=True)
    x = e.data.decode()[2:]
    if x == "x":
        await cancel_login(uid)
        await e.edit("❌ ورود لغو شد.", buttons=None)
        return await bot.send_message(uid, "منو:", buttons=main_menu())
    if x == "re":
        try:
            await send_code(uid, st)
        except errors.FloodWaitError as ex:
            return await e.answer(f"{ex.seconds} ثانیه صبر کن", alert=True)
        await e.answer("کد دوباره ارسال شد 📨")
        return await e.edit(keypad_text(st), buttons=keypad())
    if x == "b":
        st["code"] = st["code"][:-1]
    elif x != "ok":
        st["code"] += x
    if x == "ok" or len(st["code"]) >= st["length"]:
        await e.answer()
        if not st["code"]:
            return
        await e.edit(keypad_text(st) + "\n\n⏳ در حال ورود...", buttons=None)
        return await submit_code(uid)
    await e.answer()
    try:
        await e.edit(keypad_text(st), buttons=keypad())
    except errors.MessageNotModifiedError:
        pass


# =========================================================== custom diamond purchase
async def handle_buy(e, uid, t, b):
    if b["step"] == "amount":
        d = utils.digits_only(t)
        if not d:
            return await e.respond("فقط عدد بفرست؛ مثلاً: `500`")
        n = int(d)
        if n < db.MIN_BUY or n > db.MAX_BUY:
            return await e.respond(f"تعداد باید بین {db.MIN_BUY} تا {db.MAX_BUY:,} باشه.")
        price = db.price_for(n)
        b.update(step="receipt", n=n, price=price)
        pay = PAYMENT_INFO or ("اطلاعات پرداخت رو از " + (f"@{SUPPORT}" if SUPPORT else "پشتیبانی") + " بگیر.")
        return await e.respond(
            f"🧾 **فاکتور**\n💎 {n:,} الماس\n💰 مبلغ: **{price:,} تومان**\n\n{pay}\n\n"
            "بعد از پرداخت، **عکس رسید** (یا شماره پیگیری) رو همین‌جا بفرست 👇",
            buttons=[[Button.inline("❌ لغو خرید", b"bx")]])
    if b["step"] == "receipt":
        if not e.message.photo and not t:
            return
        oid = await db.order_create(uid, b["n"], b["price"])
        buy.pop(uid, None)
        s = await e.get_sender()
        who = utils.clean_name(getattr(s, "first_name", "") or "")
        cap = (f"🛒 **سفارش #{oid}**\n👤 [{who}](tg://user?id={uid}) (`{uid}`)\n"
               f"💎 {b['n']:,} الماس\n💰 {b['price']:,} تومان\n📝 {t[:300] or '—'}")
        btns = [[Button.inline("✅ تایید و شارژ", f"oa:{oid}".encode()),
                 Button.inline("❌ رد", f"or:{oid}".encode())]]
        sent = 0
        for a in ADMINS:
            try:
                await bot.send_message(a, cap, file=e.message.photo if e.message.photo else None, buttons=btns)
                sent += 1
            except Exception:
                log.exception("order notify failed for admin %s", a)
        note = "" if sent else "\n⚠️ پیام به ادمین نرسید؛ لطفاً به پشتیبانی پیام بده."
        return await e.respond(f"✅ سفارش #{oid} ثبت شد؛ بعد از تایید ادمین الماس به حسابت اضافه میشه.{note}",
                               buttons=main_menu())


@bot.on(events.CallbackQuery(pattern=rb"^bx$"))
async def cb_buy_cancel(e):
    buy.pop(e.sender_id, None)
    await e.answer("لغو شد")
    await e.edit("❌ خرید لغو شد.", buttons=None)


@bot.on(events.CallbackQuery(pattern=rb"^o[ar]:"))
async def cb_order(e):
    if e.sender_id not in ADMINS:
        return await e.answer("فقط ادمین", alert=True)
    kind, oid = e.data.decode().split(":")
    ok = kind == "oa"
    row = await db.order_resolve(int(oid), "approved" if ok else "rejected")
    if not row:
        return await e.answer("این سفارش قبلاً بررسی شده", alert=True)
    uid, n = row["user_id"], row["diamonds"]
    if ok:
        bal = await db.add(uid, n, "purchase")
        try:
            await bot.send_message(uid, f"✅ پرداختت تایید شد؛ **{n:,}** 💎 به حسابت اضافه شد.\n"
                                        f"💎 موجودی: {db.fmt(uid, bal)}")
        except Exception:
            pass
    else:
        try:
            await bot.send_message(uid, f"❌ سفارش #{oid} رد شد. اگه اشتباهه به پشتیبانی پیام بده.")
        except Exception:
            pass
    await e.answer("انجام شد")
    try:
        cap = (e.message.message or "") + ("\n\n✅ تایید شد" if ok else "\n\n❌ رد شد")
        await e.edit(cap, buttons=None)
    except Exception:
        pass


# =========================================================== private messages
@bot.on(events.NewMessage(func=lambda e: e.is_private))
async def on_private(e):
    uid = e.sender_id
    t = (e.raw_text or "").strip()
    if t.startswith("/"):
        return
    b = buy.get(uid)
    if b:
        return await handle_buy(e, uid, t, b)
    st = login.get(uid)
    if not st:
        return

    if st["step"] == "phone":
        if t == "❌ انصراف":
            await cancel_login(uid)
            await e.respond("لغو شد.", buttons=Button.clear())
            return await e.respond("منو:", buttons=main_menu())
        contact = e.message.contact
        if contact:
            if contact.user_id and contact.user_id != uid:
                return await e.respond("⚠️ فقط شماره‌ی خودت رو بفرست (دکمه‌ی «ارسال خودکار شماره»).")
            raw = contact.phone_number
        else:
            raw = t
        phone = utils.normalize_phone(raw)
        if not phone:
            return await e.respond("⚠️ شماره معتبر نیست. هر فرمتی قبوله: `09121234567` یا `+989121234567` یا `00989121234567`")
        st["phone"] = phone
        try:
            c = TelegramClient(StringSession(), API_ID, API_HASH)
            await c.connect()
            st["client"] = c
            await send_code(uid, st)
        except errors.PhoneNumberInvalidError:
            await cancel_login(uid)
            return await e.respond("⚠️ این شماره درست نیست.", buttons=Button.clear())
        except errors.PhoneNumberBannedError:
            await cancel_login(uid)
            return await e.respond("⛔ این شماره توسط تلگرام مسدود شده.", buttons=Button.clear())
        except errors.FloodWaitError as ex:
            await cancel_login(uid)
            return await e.respond(f"⏳ تلگرام محدودت کرد. {ex.seconds} ثانیه بعد امتحان کن.", buttons=Button.clear())
        except Exception as ex:
            await cancel_login(uid)
            log.exception("send_code failed")
            return await e.respond(f"⚠️ خطا: {type(ex).__name__}", buttons=Button.clear())
        await e.respond(f"📨 کد برای `{phone}` ارسال شد.", buttons=Button.clear())
        return await e.respond(keypad_text(st), buttons=keypad())

    if st["step"] == "code":
        digits = utils.digits_only(t)  # accepts "1 2 3 4 5", "12-345", Persian digits, ...
        if not digits:
            return await e.respond("کد رو با دکمه‌های عددی بزن 👆")
        try:
            await e.delete()
        except Exception:
            pass
        st["code"] = digits
        return await submit_code(uid)

    if st["step"] == "password":
        try:
            await e.delete()
        except Exception:
            pass
        try:
            await st["client"].sign_in(password=t)
        except errors.PasswordHashInvalidError:
            return await e.respond("❌ رمز اشتباهه. دوباره بفرست.")
        except Exception as ex:
            await cancel_login(uid)
            return await e.respond(f"⚠️ خطا: {type(ex).__name__}", buttons=main_menu())
        await complete_login(uid)


# =========================================================== menu
@bot.on(events.NewMessage(pattern=r"^/start(@\w+)?$", func=lambda e: e.is_private))
async def on_start(e):
    uid = e.sender_id
    buy.pop(uid, None)
    s = await e.get_sender()
    got = await db.claim_welcome(uid, utils.clean_name(getattr(s, "first_name", "")))
    m = await e.respond(WELCOME, file=await assets.banner(), buttons=main_menu())
    assets.remember(m)
    if got:
        bal = db.fmt(uid, await db.balance(uid))
        await e.respond(f"🎁 **{db.WELCOME_BONUS} الماس** هدیه‌ی اولین ورودت به حسابت اضافه شد!\n💎 موجودی: {bal}")


@bot.on(events.CallbackQuery(pattern=rb"^m:"))
async def cb_menu(e):
    uid = e.sender_id
    act = e.data.decode()[2:]
    if act == "login":
        if selfbot.clients.get(uid):
            return await e.answer("سلفت همین الان فعاله ✅ (داخل چتی .panel بزن)", alert=True)
        if await db.balance(uid) < db.LOGIN_FEE:
            return await e.answer(f"💎 الماس کافی نیست؛ هر ورود سلف {db.LOGIN_FEE} الماس لازم داره.", alert=True)
        await cancel_login(uid)
        st = {"step": "phone", "code": "", "length": 5}
        st["task"] = asyncio.create_task(login_timeout(uid, st))
        login[uid] = st
        await e.answer()
        return await bot.send_message(
            uid, "📲 **شماره‌ات رو بفرست**\nدکمه‌ی «ارسال خودکار شماره» رو بزن، یا خودت تایپ کن (هر فرمتی قبوله).",
            buttons=[[Button.request_phone("📲 ارسال خودکار شماره", resize=True, single_use=True)],
                     [Button.text("❌ انصراف", resize=True, single_use=True)]])
    if act == "bal":
        return await e.answer(f"💎 موجودی: {db.fmt(uid, await db.balance(uid))}", alert=True)
    if act == "status":
        await e.answer()
        return await bot.send_message(uid, await selfbot.status_text(uid))
    if act == "buy":
        await e.answer()
        buy[uid] = {"step": "amount"}
        return await bot.send_message(uid, price_text(), buttons=[[Button.inline("❌ لغو", b"bx")]])
    if act == "logout":
        await e.answer()
        c = selfbot.clients.get(uid)
        await selfbot.stop_user(uid)  # restores name/bio if the clock was on
        if c:
            try:
                await c.connect()
                await c.log_out()
            except Exception:
                pass
        await db.user_remove(uid)
        return await bot.send_message(uid, "🚪 سلف حذف شد و نشست از تلگرام هم خارج شد.", buttons=main_menu())
    if act == "help":
        await e.answer()
        return await bot.send_message(uid, selfbot.HELP + "\n\n" + groupgames.GAME_HELP)


# =========================================================== inline panel
async def panel_view(uid):
    text = await selfbot.status_text(uid)
    rows = []
    for key, label, default in selfbot.TOGGLES:
        on = await db.uflag(uid, key, default)
        rows.append([Button.inline(f"{label}: {'🟢' if on else '🔴'}", f"p:{uid}:t_{key}".encode())])
    font = int(await db.uget(uid, "clock_font", "1")) % len(selfbot.FONTS)
    loc = await db.uget(uid, "clock_loc", "name")
    rows.append([Button.inline(f"🔤 فونت ساعت: {selfbot.style_time('12:34', font)}", f"p:{uid}:font".encode()),
                 Button.inline(f"📍 جای ساعت: {selfbot.LOC_LABEL.get(loc, 'اسم')}", f"p:{uid}:loc".encode())])
    rows.append([Button.inline("🔃 بروزرسانی", f"p:{uid}:refresh".encode()),
                 Button.inline("📖 راهنما", f"p:{uid}:help".encode())])
    rows.append([Button.inline("✖️ بستن", f"p:{uid}:close".encode())])
    return text, rows


@bot.on(events.InlineQuery)
async def on_inline(e):
    uid = e.sender_id
    if e.text.strip() != "panel" or not await db.user_get(uid):
        return
    text, rows = await panel_view(uid)
    res = None
    if assets.BANNER_PATH:
        try:
            res = await e.builder.photo(assets.PANEL_PHOTO or assets.BANNER_PATH, text=text, buttons=rows)
            if assets.PANEL_PHOTO is None:
                assets.PANEL_PHOTO = getattr(res, "photo", None)
        except Exception:
            log.exception("banner inline failed, falling back to text")
            res = None
    if res is None:
        res = e.builder.article("🛠 پنل سلف", text=text, buttons=rows)
    await e.answer([res], cache_time=0, private=True)


@bot.on(events.CallbackQuery(pattern=rb"^p:"))
async def cb_panel(e):
    _, uid_s, act = e.data.decode().split(":", 2)
    uid = int(uid_s)
    if e.sender_id != uid:
        return await e.answer("این پنل برای تو نیست 😉", alert=True)
    try:
        if act.startswith("t_"):
            key = act[2:]
            default = next((d for k, _, d in selfbot.TOGGLES if k == key), False)
            new = not await db.uflag(uid, key, default)
            if key == "clock":
                await selfbot.set_clock(uid, new)
            else:
                await db.uset_flag(uid, key, new)
        elif act == "font":
            await selfbot.cycle_font(uid)
        elif act == "loc":
            await selfbot.cycle_loc(uid)
        elif act == "close":
            return await e.edit("✖️ پنل بسته شد.", buttons=None)
        elif act == "help":
            return await e.edit(selfbot.HELP, buttons=[[Button.inline("⬅️ برگشت", f"p:{uid}:refresh".encode())]])
    except Exception as ex:
        log.exception("panel action failed")
        await e.answer(f"⚠️ {type(ex).__name__}", alert=True)
    text, rows = await panel_view(uid)
    try:
        await e.edit(text, buttons=rows)
    except errors.MessageNotModifiedError:
        pass
    try:
        await e.answer()
    except Exception:
        pass


# =========================================================== admin
def is_admin(e):
    return e.is_private and e.sender_id in ADMINS


@bot.on(events.NewMessage(pattern=r"^/addd\s+(\d+)\s+(-?\d+)$", func=is_admin))
async def adm_add(e):
    uid, n = int(e.pattern_match.group(1)), int(e.pattern_match.group(2))
    bal = await db.add(uid, n, "admin")
    await e.respond(f"✅ {n} 💎 به `{uid}` اضافه شد. موجودی: {bal:,}")
    try:
        await bot.send_message(uid, f"💎 {n} الماس به حسابت اضافه شد. موجودی: {bal:,}")
    except Exception:
        pass


@bot.on(events.NewMessage(pattern=r"^/users$", func=is_admin))
async def adm_users(e):
    total, active = await db.users_count()
    await e.respond(f"👥 کل کاربران: {total}\n✅ سلف ثبت‌شده: {active}\n🟢 در حال اجرا: {len(selfbot.clients)}")


# =========================================================== boot
async def main():
    await db.init()
    await bot.start(bot_token=BOT_TOKEN)
    groupgames.register(bot, ADMINS)
    await groupgames.recover(bot)
    for r in await db.users_active_list():
        uid = r["user_id"]
        try:
            ok = await selfbot.start_user(uid, secure.dec(r["session"]), API_ID, API_HASH)
            if not ok:
                await db.user_remove(uid)
                await bot.send_message(uid, "⚠️ نشست سلفت دیگه معتبر نیست (از تلگرام خارج شدی). دوباره وارد شو.",
                                       buttons=main_menu())
        except Exception:
            log.exception("failed to start self for %s", uid)
    log.info("running: %s selfs", len(selfbot.clients))
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
