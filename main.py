"""Manager bot: onboarding (phone + keypad code login), panel, wallet, admin tools, game group."""
import asyncio
import logging
import os

from telethon import Button, TelegramClient, errors, events
from telethon.sessions import StringSession

import assets
import catalog
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
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
db.ADMINS.update(ADMINS)
selfbot.BOT_USERNAME = BOT_USERNAME
SUPPORT = os.getenv("SUPPORT_USERNAME", "").lstrip("@")
CARD_NUMBER = os.getenv("CARD_NUMBER", "6219861455298157")
CARD_OWNER = os.getenv("CARD_OWNER", "دلیر")
BUY_MIN, BUY_MAX = int(os.getenv("BUY_MIN", "100")), 1_000_000
buy: dict[int, dict] = {}                       # uid -> {"n": "<digits typed so far>"}
pending: dict[int, tuple] = {}                  # uid -> ("code",) | ("receipt", amount)

bot = TelegramClient(StringSession(), API_ID, API_HASH)
login: dict[int, dict] = {}

WELCOME = """👋 **به ربات سلف خوش اومدی!**

با ساخت سلف، ابزارهای زیادی روی اکانت خودت فعال میشه (ضد حذف، تایمردار، ترجمه، ساعت، منشی و...).
بعد از ورود، داخل هر چتی `.پنل` بزن تا پنل مدیریت باز بشه.

🔐 **امنیت:** نشست (session) اکانتت رمزنگاری‌شده ذخیره میشه. هر لحظه با «خروج و حذف سلف» نشست از سرور پاک و از تلگرام هم خارج میشه.
💎 هر ۱۵۰۰ الماس = ۳۰ روز فعال بودن سلف.
🎁 هدیه‌ی ورود: **{welcome}** الماس رایگان | هر ورود سلف به اکانت: **{cost}** الماس
💳 قیمت: ۱۵۰۰ الماس = {price} تومان"""


def welcome_text():
    return WELCOME.format(welcome=db.WELCOME_BONUS, cost=db.LOGIN_COST, price=f"{db.price_toman(1500):,}")


def main_menu():
    return [
        [Button.inline(f"📲 ساخت / ورود سلف ({db.LOGIN_COST} 💎)", b"m:login")],
        [Button.inline("💎 موجودی", b"m:bal"), Button.inline("📊 وضعیت", b"m:status")],
        [Button.inline("🎁 هدیه روزانه", b"m:daily"), Button.inline("🎟 کد هدیه", b"m:code")],
        [Button.inline("💳 خرید الماس", b"m:buy"), Button.inline("👥 دعوت دوستان", b"m:ref")],
        [Button.inline(f"📖 همه‌ی دستورها و راهنما ({catalog.feature_count()} قابلیت)", b"m:help")],
        [Button.inline("🔄 تمدید ۳۰ روزه (۱۵۰۰ 💎)", b"m:renew"), Button.inline("🧾 تراکنش‌ها", b"m:hist")],
        [Button.inline("🚪 خروج و حذف سلف", b"m:logout")],
    ]


def rows_to_buttons(rows):
    return [[Button.inline(label, data if isinstance(data, bytes) else data.encode()) for label, data in r] for r in rows]


def need_diamonds_text(bal):
    t = (f"⛔ برای ورود سلف به اکانتت **{db.LOGIN_COST}** الماس لازمه ولی موجودیت **{db.fmt(bal)}** هست.\n\n"
         f"💳 ۱۵۰۰ الماس = {db.price_toman(1500):,} تومان\n"
         "از دکمه‌ی «خرید الماس» استفاده کن یا هدیه‌ی روزانه / کد هدیه / دعوت دوستان رو بزن.")
    return t


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
    c = st["client"]
    sent = await c.send_code_request(st["phone"])
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
    # every self login costs LOGIN_COST diamonds (owner is free). No diamonds -> self never enters the account.
    paid = await db.try_spend(uid, db.LOGIN_COST, "self_login")
    if paid is None:
        try:
            await c.log_out()
        except Exception:
            pass
        return await bot.send_message(uid, need_diamonds_text(await db.balance(uid)),
                                      buttons=[[Button.inline("💳 خرید الماس", b"m:buy")]])
    session = c.session.save()
    await c.disconnect()
    await db.user_upsert(uid, st["phone"], secure.enc(session))
    ok = await selfbot.start_user(uid, session, API_ID, API_HASH)
    if not ok:
        await db.add(uid, db.LOGIN_COST, "self_login_refund")
        return await bot.send_message(uid, "⚠️ ورود انجام شد ولی سلف بالا نیومد؛ الماس‌هات برگشت داده شد. دوباره امتحان کن.")
    cost = "رایگان (مالک)" if db.is_admin(uid) else f"{db.LOGIN_COST} 💎 کسر شد"
    await bot.send_message(
        uid, f"✅ **سلف فعال شد!** ({cost})\nداخل هر چتی `.پنل` بزن، یا `.راهنما` برای لیست دستورها.\n\n"
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
        return await bot.send_message(uid, "❌ کد اشتباهه. دوباره با دکمه‌ها بزن:",
                                      buttons=keypad())
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


@bot.on(events.NewMessage(func=lambda e: e.is_private))
async def on_private(e):
    uid = e.sender_id
    t = (e.raw_text or "").strip()
    st = login.get(uid)
    if not st or t.startswith("/"):
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
@bot.on(events.NewMessage(pattern=r"^/start(@\w+)?(?:\s+(\S+))?$", func=lambda e: e.is_private))
async def on_start(e):
    uid = e.sender_id
    s_ = await e.get_sender()
    name = utils.clean_name(getattr(s_, "first_name", ""))
    arg = e.pattern_match.group(2) or ""
    ref = int(arg[3:]) if arg.startswith("ref") and arg[3:].isdigit() else None
    granted, rewarded = await db.welcome_bonus(uid, name, ref)
    m = await e.respond(welcome_text(), file=await assets.banner(), buttons=main_menu())
    assets.remember(m)
    if granted:
        await e.respond(f"🎉 **{db.WELCOME_BONUS}** الماس هدیه‌ی اولین ورودت به حسابت اضافه شد!\n"
                        f"💎 موجودی: {db.fmt(await db.balance(uid))}")
        if rewarded:
            try:
                await bot.send_message(ref, f"👥 یه نفر با لینک تو وارد ربات شد! +{db.REF_BONUS} 💎 گرفتی.")
            except Exception:
                pass


@bot.on(events.NewMessage(pattern=r"^/(daily|bal|code|buy|history)(@\w+)?(?:\s+(\S+))?$", func=lambda e: e.is_private))
async def on_cmds(e):
    uid, c = e.sender_id, e.pattern_match.group(1)
    arg = e.pattern_match.group(3)
    if c == "bal" and arg and uid in ADMINS:
        return                           # admin form "/bal <id>" is handled separately
    await db.ensure_wallet(uid, utils.clean_name(getattr(await e.get_sender(), "first_name", "")))
    if c == "daily":
        return await e.respond(await do_daily(uid))
    if c == "bal":
        return await e.respond(f"💎 موجودی: **{db.fmt(await db.balance(uid))}**")
    if c == "code":
        if not arg:
            pending[uid] = ("code",)
            return await e.respond("🎟 کد هدیه‌ات رو بفرست:")
        return await e.respond(await do_code(uid, arg))
    if c == "buy":
        return await start_buy(uid)
    if c == "history":
        return await e.respond(await hist_text(uid))


async def do_daily(uid):
    bal, wait = await db.claim_daily(uid, None)
    if bal is None:
        return f"⏳ هدیه‌ی بعدی: {wait // 3600} ساعت و {wait % 3600 // 60} دقیقه دیگه"
    return f"🎁 {db.DAILY_BONUS} 💎 هدیه‌ی روزانه گرفتی!\n💎 موجودی: {db.fmt(bal)}"


async def do_code(uid, code):
    r = await db.code_redeem(uid, code.strip())
    if r == "none":
        return "❌ این کد وجود نداره."
    if r == "used":
        return "⚠️ این کد رو قبلاً استفاده کردی."
    if r == "full":
        return "⌛ ظرفیت این کد تموم شده."
    return f"🎉 کد فعال شد! +{r:,} 💎\n💎 موجودی: {db.fmt(await db.balance(uid))}"


REASONS = {"welcome": "🎁 هدیه ورود", "daily": "🎁 هدیه روزانه", "referral": "👥 دعوت", "admin": "👑 ادمین",
           "renew": "🔄 تمدید", "self_login": "📲 ورود سلف", "purchase": "💳 خرید", "transfer_in": "📥 دریافت",
           "transfer_out": "📤 انتقال", "game_bet": "🎲 شرط بازی", "game_win": "🏆 برد بازی",
           "game_refund": "↩️ برگشت شرط"}


async def hist_text(uid):
    rows = await db.history(uid, 12)
    if not rows:
        return "🧾 هنوز تراکنشی نداری."
    lines = []
    for r in rows:
        why = next((v for k, v in REASONS.items() if r["reason"].startswith(k)), r["reason"])
        lines.append(f"{'➕' if r['amount'] > 0 else '➖'} {abs(r['amount']):,} — {why}")
    return "🧾 **آخرین تراکنش‌ها**\n" + "\n".join(lines) + f"\n\n💎 موجودی: {db.fmt(await db.balance(uid))}"


@bot.on(events.CallbackQuery(pattern=rb"^m:"))
async def cb_menu(e):
    uid = e.sender_id
    act = e.data.decode()[2:]
    if act == "login":
        if selfbot.clients.get(uid) and await db.user_is_active(uid):
            return await e.answer("سلفت همین الان فعاله ✅ (داخل چتی .panel بزن)", alert=True)
        bal = await db.balance(uid)
        if bal < db.LOGIN_COST:
            await e.answer("⛔ الماس کافی نداری", alert=True)
            return await bot.send_message(uid, need_diamonds_text(bal),
                                          buttons=[[Button.inline("💳 خرید الماس", b"m:buy")]])
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
        return await e.answer(f"💎 موجودی: {db.fmt(await db.balance(uid))}", alert=True)
    if act == "status":
        await e.answer()
        return await bot.send_message(uid, await selfbot.status_text(uid))
    if act == "renew":
        if not await db.user_get(uid):
            return await e.answer("اول سلف بساز", alert=True)
        if not await db.renew(uid):
            return await e.answer(f"الماس کافی نیست ({db.DIAMONDS_PER_30_DAYS} 💎 لازمه)", alert=True)
        await e.answer("✅ ۳۰ روز تمدید شد", alert=True)
        return await bot.send_message(uid, await selfbot.status_text(uid))
    if act == "logout":
        await e.answer()
        c = selfbot.clients.get(uid)
        if c:
            try:
                await c.log_out()
            except Exception:
                pass
        await selfbot.stop_user(uid)
        await db.user_remove(uid)
        return await bot.send_message(uid, "🚪 سلف حذف شد و نشست از تلگرام هم خارج شد.", buttons=main_menu())
    if act == "help":
        await e.answer()
        text, rows = catalog.view("", lambda p: f"h:{p}", "m:menu")
        return await bot.send_message(uid, text, buttons=rows_to_buttons(rows))
    if act == "menu":
        await e.answer()
        return await e.edit("منو:", buttons=main_menu())
    if act == "daily":
        await e.answer()
        return await bot.send_message(uid, await do_daily(uid))
    if act == "code":
        await e.answer()
        pending[uid] = ("code",)
        return await bot.send_message(uid, "🎟 کد هدیه‌ات رو بفرست:")
    if act == "buy":
        await e.answer()
        return await start_buy(uid)
    if act == "ref":
        await e.answer()
        n = await db.invited_count(uid)
        return await bot.send_message(
            uid, f"👥 **دعوت دوستان**\nبه ازای هر نفر که با لینک تو وارد ربات بشه **{db.REF_BONUS}** 💎 می‌گیری.\n\n"
                 f"🔗 `https://t.me/{BOT_USERNAME}?start=ref{uid}`\n\n👤 دعوت‌شده‌ها: {n}")
    if act == "hist":
        await e.answer()
        return await bot.send_message(uid, await hist_text(uid))


# =========================================================== help (buttons for every feature)
async def run_action(uid, key):
    """Execute a feature straight from its help button."""
    if key == "status":
        txt = await selfbot.status_text(uid)
    elif key == "bal":
        txt = f"💎 موجودی: **{db.fmt(await db.balance(uid))}**"
    elif key == "daily":
        txt = await do_daily(uid)
    elif key == "top":
        rows = await db.top(10)
        txt = "🏆 **برترین‌ها**\n" + ("\n".join(
            f"{i}. {utils.clean_name(r['name'] or str(r['user_id']))} — {r['diamonds']:,} 💎"
            for i, r in enumerate(rows, 1)) or "هنوز کسی نیست")
    elif key == "date":
        txt = selfbot.date_text()
    elif key == "ref":
        txt = (f"👥 لینک دعوت (هر نفر {db.REF_BONUS} 💎):\n`https://t.me/{BOT_USERNAME}?start=ref{uid}`\n"
               f"دعوت‌شده‌ها: {await db.invited_count(uid)}")
    elif key == "buy":
        return await start_buy(uid)
    else:
        return
    await bot.send_message(uid, txt)


@bot.on(events.CallbackQuery(pattern=rb"^h:"))
async def cb_help(e):
    path = e.data.decode()[2:]
    if path.startswith("r"):
        await e.answer()
        return await run_action(e.sender_id, path[1:])
    text, rows = catalog.view(path, lambda p: f"h:{p}", "m:menu")
    await e.answer()
    try:
        await e.edit(text, buttons=rows_to_buttons(rows))
    except errors.MessageNotModifiedError:
        pass


# =========================================================== buying diamonds: keypad -> live price -> card -> receipt
def buy_screen(n_str):
    n = int(n_str or 0)
    price = db.price_toman(n) if n else 0
    text = ("💳 **خرید الماس**\n\n⌨️ تعداد الماس مورد نظرت رو با دکمه‌ها وارد کن:\n\n"
            f"💎 تعداد: **{n:,}**\n💰 مبلغ قابل پرداخت: **{price:,} تومان**\n\n"
            f"ℹ️ هر ۱۵۰۰ الماس = {db.price_toman(1500):,} تومان | حداقل خرید {BUY_MIN} الماس")
    k = lambda t, d: Button.inline(t, d.encode())
    rows = [[k(str(n_), f"bk:{n_}") for n_ in r] for r in ((1, 2, 3), (4, 5, 6), (7, 8, 9))]
    rows.append([k("⌫", "bk:b"), k("0", "bk:0"), k("000", "bk:000")])
    rows.append([k("💎 1500", "bk:p1500"), k("💎 3000", "bk:p3000"), k("💎 7500", "bk:p7500")])
    rows.append([k("🗑 پاک کردن", "bk:c"), k("✅ تایید تعداد", "bk:ok")])
    rows.append([k("❌ لغو", "bk:x")])
    return text, rows


async def start_buy(uid):
    buy[uid] = {"n": ""}
    text, rows = buy_screen("")
    await bot.send_message(uid, text, buttons=rows)


@bot.on(events.CallbackQuery(pattern=rb"^bk:"))
async def cb_buykey(e):
    uid = e.sender_id
    st = buy.setdefault(uid, {"n": ""})
    x = e.data.decode()[3:]
    if x == "x":
        buy.pop(uid, None)
        await e.answer()
        return await e.edit("❌ خرید لغو شد.", buttons=[[Button.inline("⬅️ منو", b"m:menu")]])
    if x == "ok":
        n = int(st["n"] or 0)
        if n < BUY_MIN:
            return await e.answer(f"حداقل خرید {BUY_MIN} الماسه", alert=True)
        await e.answer()
        return await e.edit(
            invoice_text(n),
            buttons=[[Button.inline("✅ پرداخت کردم", f"bp:{n}".encode())],
                     [Button.inline("✏️ تغییر تعداد", b"bk:back"), Button.inline("❌ لغو", b"bk:x")]])
    if x == "back":
        pass
    elif x == "b":
        st["n"] = st["n"][:-1]
    elif x == "c":
        st["n"] = ""
    elif x.startswith("p"):
        st["n"] = x[1:]
    else:                                  # digit or "000"
        new = (st["n"] + x).lstrip("0")
        if new and int(new) <= BUY_MAX:
            st["n"] = new
        else:
            return await e.answer(f"حداکثر {BUY_MAX:,} الماس", alert=True)
    text, rows = buy_screen(st["n"])
    try:
        await e.edit(text, buttons=rows)
    except errors.MessageNotModifiedError:
        pass
    await e.answer()


def invoice_text(n):
    return (f"🧾 **فاکتور خرید**\n\n💎 تعداد: **{n:,}** الماس\n💰 مبلغ: **{db.price_toman(n):,} تومان**\n\n"
            f"💳 شماره کارت (لمس کن تا کپی بشه):\n`{CARD_NUMBER}`\n👤 به نام: **{CARD_OWNER}**\n\n"
            "⚠️ دقیقاً همین مبلغ رو کارت‌به‌کارت کن، بعدش دکمه‌ی «✅ پرداخت کردم» رو بزن.")


def _valid_n(x):
    n = int(x)
    return n if BUY_MIN <= n <= BUY_MAX else None


@bot.on(events.CallbackQuery(pattern=rb"^bp:"))
async def cb_paid(e):
    n = _valid_n(e.data.decode()[3:])
    if not n:
        return await e.answer("مقدار نامعتبره", alert=True)
    await e.answer()
    await e.edit(invoice_text(n) + "\n\n✅ بعد از پرداخت، روی «🧾 ارسال رسید» بزن و **عکس رسید** رو بفرست.",
                 buttons=[[Button.inline("🧾 ارسال رسید", f"br:{n}".encode())],
                          [Button.inline("❌ لغو", b"bk:x")]])


@bot.on(events.CallbackQuery(pattern=rb"^br:"))
async def cb_receipt_ask(e):
    uid = e.sender_id
    n = _valid_n(e.data.decode()[3:])
    if not n:
        return await e.answer("مقدار نامعتبره", alert=True)
    if await db.receipts_pending(uid) >= 3:
        return await e.answer("۳ رسید در انتظار بررسی داری؛ صبر کن ادمین بررسی کنه.", alert=True)
    pending[uid] = ("receipt", n)
    await e.answer()
    await bot.send_message(uid, f"🧾 **عکس رسید پرداخت ({db.price_toman(n):,} تومان)** رو همین‌جا بفرست.\n"
                                "(می‌تونی عکس، اسکرین‌شات یا شماره‌ی پیگیری بفرستی)\n❌ لغو: /start")


@bot.on(events.NewMessage(func=lambda e: e.is_private and e.sender_id in pending))
async def on_pending(e):
    uid = e.sender_id
    t = (e.raw_text or "").strip()
    if t.startswith("/"):
        if t.split()[0].split("@")[0] == "/start":
            pending.pop(uid, None)       # /start cancels; other commands leave the state alone
        return
    kind = pending[uid]
    if kind[0] == "code":
        pending.pop(uid)
        return await e.respond(await do_code(uid, t))
    if kind[0] == "receipt":
        if not (e.photo or t or e.document):
            return
        pending.pop(uid)
        rid = await db.receipt_create(uid, kind[1])
        who = await e.get_sender()
        head = (f"🧾 **رسید جدید #{rid}**\n👤 {utils.clean_name(getattr(who, 'first_name', ''))} (`{uid}`)\n"
                f"💎 {kind[1]:,} الماس — 💰 {db.price_toman(kind[1]):,} تومان\n"
                f"💳 کارت: {CARD_NUMBER} ({CARD_OWNER})\nرسید رو چک کن و تایید یا رد کن 👇")
        sent = 0
        for a in ADMINS:
            try:
                await bot.forward_messages(a, e.message)
                await bot.send_message(a, head, buttons=[[Button.inline(f"✅ تایید و شارژ {kind[1]:,}", f"rc:ok:{rid}".encode()),
                                                          Button.inline("❌ رد", f"rc:no:{rid}".encode())]])
                sent += 1
            except Exception:
                log.exception("receipt forward failed")
        await e.respond("✅ رسیدت برای ادمین ارسال شد. بعد از تایید، الماس‌ها خودکار واریز میشن."
                        if sent else "⚠️ ارسال برای ادمین ناموفق بود، به پشتیبانی پیام بده.")


@bot.on(events.CallbackQuery(pattern=rb"^rc:", func=lambda e: e.sender_id in ADMINS))
async def cb_receipt(e):
    _, act, rid = e.data.decode().split(":")
    row = await db.receipt_resolve(int(rid), "approved" if act == "ok" else "rejected")
    if not row:
        return await e.answer("این رسید قبلاً بررسی شده", alert=True)
    if act == "ok":
        bal = await db.add(row["user_id"], row["amount"], "purchase")
        await e.edit(f"✅ رسید #{rid} تایید شد: {row['amount']:,} 💎 → `{row['user_id']}`", buttons=None)
        try:
            await bot.send_message(row["user_id"], f"✅ پرداختت تایید شد! +{row['amount']:,} 💎\n💎 موجودی: {db.fmt(bal)}")
        except Exception:
            pass
    else:
        await e.edit(f"❌ رسید #{rid} رد شد.", buttons=None)
        try:
            await bot.send_message(row["user_id"], "❌ رسیدت تایید نشد. اگه اشتباهی شده به پشتیبانی پیام بده.")
        except Exception:
            pass


# =========================================================== inline panel
async def panel_view(uid):
    text = await selfbot.status_text(uid)
    rows, line = [], []
    for key, label, default in selfbot.TOGGLES:
        on = await db.uflag(uid, key, default)
        line.append(Button.inline(f"{label}: {'🟢' if on else '🔴'}", f"p:{uid}:t_{key}".encode()))
        if len(line) == 2:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    rows.append([Button.inline("🔄 تمدید ۳۰ روزه", f"p:{uid}:renew".encode()),
                 Button.inline("🔃 بروزرسانی", f"p:{uid}:refresh".encode())])
    rows.append([Button.inline("📖 راهنما", f"p:{uid}:help".encode()),
                 Button.inline("✖️ بستن", f"p:{uid}:close".encode())])
    return text, rows


@bot.on(events.InlineQuery)
async def on_inline(e):
    uid = e.sender_id
    if e.text.strip() != "panel" or not await db.user_get(uid):
        return
    text, rows = await panel_view(uid)
    if assets.BANNER_PATH:
        res = await e.builder.photo(assets.PANEL_PHOTO or assets.BANNER_PATH, text=text, buttons=rows)
        if assets.PANEL_PHOTO is None:
            assets.PANEL_PHOTO = getattr(res, "photo", None)
    else:
        res = e.builder.article("🛠 پنل سلف", text=text, buttons=rows)
    await e.answer([res], cache_time=0, private=True)


@bot.on(events.CallbackQuery(pattern=rb"^p:"))
async def cb_panel(e):
    _, uid_s, act = e.data.decode().split(":", 2)
    uid = int(uid_s)
    if e.sender_id != uid:
        return await e.answer("این پنل برای تو نیست 😉", alert=True)
    if act.startswith("t_"):
        key = act[2:]
        default = next((d for k, _, d in selfbot.TOGGLES if k == key), False)
        new = not await db.uflag(uid, key, default)
        if key == "clock":
            await selfbot.set_clock(uid, new)
        else:
            await db.uset_flag(uid, key, new)
    elif act == "renew":
        if not await db.renew(uid):
            return await e.answer(f"الماس کافی نیست ({db.DIAMONDS_PER_30_DAYS} 💎 لازمه)", alert=True)
        await e.answer("✅ تمدید شد", alert=True)
    elif act == "close":
        return await e.edit("✖️ پنل بسته شد.", buttons=None)
    elif act == "help":
        text, rows = catalog.view("", lambda p: f"p:{uid}:H{p}", f"p:{uid}:refresh")
        await e.answer()
        return await e.edit(text, buttons=rows_to_buttons(rows))
    elif act.startswith("H"):
        path = act[1:]
        if path.startswith("r"):
            await e.answer()
            return await run_action(uid, path[1:])
        text, rows = catalog.view(path, lambda p: f"p:{uid}:H{p}", f"p:{uid}:refresh")
        await e.answer()
        try:
            return await e.edit(text, buttons=rows_to_buttons(rows))
        except errors.MessageNotModifiedError:
            return
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
    await e.respond(f"✅ {n} 💎 به `{uid}` اضافه شد. موجودی: {db.fmt(bal)}")
    try:
        await bot.send_message(uid, f"💎 {n} الماس به حسابت اضافه شد. موجودی: {db.fmt(bal)}")
    except Exception:
        pass


@bot.on(events.NewMessage(pattern=r"^/take\s+(\d+)\s+(\d+)$", func=is_admin))
async def adm_take(e):
    uid, n = int(e.pattern_match.group(1)), int(e.pattern_match.group(2))
    if await db.try_spend(uid, n, "admin_take") is None:
        return await e.respond("⚠️ موجودی اون کاربر کمتر از این مقداره.")
    await e.respond(f"✅ {n} 💎 از `{uid}` کم شد. موجودی: {db.fmt(await db.balance(uid))}")


@bot.on(events.NewMessage(pattern=r"^/bal\s+(\d+)$", func=is_admin))
async def adm_bal(e):
    uid = int(e.pattern_match.group(1))
    await e.respond(f"💎 موجودی `{uid}`: {db.fmt(await db.balance(uid))}")


@bot.on(events.NewMessage(pattern=r"^/gift\s+(\S+)\s+(\d+)\s+(\d+)$", func=is_admin))
async def adm_gift(e):
    code, amount, uses = e.pattern_match.group(1), int(e.pattern_match.group(2)), int(e.pattern_match.group(3))
    await db.code_create(code, amount, uses)
    await e.respond(f"🎟 کد `{code.lower()}` ساخته شد: {amount:,} 💎 برای {uses} نفر.\nکاربر: `/code {code.lower()}`")


@bot.on(events.NewMessage(pattern=r"^/bc\s+(.+)$", func=is_admin))
async def adm_broadcast(e):
    text, ok = e.pattern_match.group(1), 0
    for r in await db.pool.fetch("SELECT user_id FROM wallets"):
        try:
            await bot.send_message(r["user_id"], text)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await e.respond(f"📣 برای {ok} نفر ارسال شد.")


@bot.on(events.NewMessage(pattern=r"^/users$", func=is_admin))
async def adm_users(e):
    total, active = await db.users_count()
    await e.respond(f"👥 کل کاربران: {total}\n✅ اشتراک فعال: {active}\n🟢 سلف در حال اجرا: {len(selfbot.clients)}")


# =========================================================== background / boot
async def expiry_loop():
    while True:
        await asyncio.sleep(600)
        try:
            for r in await db.users_active_list():
                uid = r["user_id"]
                if not await db.user_is_active(uid) and await db.uget(uid, "exp_notified", "0") != "1":
                    await db.uset(uid, "exp_notified", "1")
                    try:
                        await bot.send_message(
                            uid, "⛔ اشتراک سلفت تموم شد. با ۱۵۰۰ 💎 تمدید کن (دکمه‌ی تمدید یا `.تمدید`).",
                            buttons=main_menu())
                    except Exception:
                        pass
        except Exception:
            log.exception("expiry loop")


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
    asyncio.create_task(expiry_loop())
    log.info("running: %s selfs", len(selfbot.clients))
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
