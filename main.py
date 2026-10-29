"""Manager bot: onboarding (phone + keypad code login), panel, wallet, admin tools, game group."""
import asyncio
import html
import logging
import os
import time

from telethon import Button, TelegramClient, errors, events
from telethon.sessions import StringSession

import assets
import botapi
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
CHANNEL = os.getenv("CHANNEL_USERNAME", "").lstrip("@")          # shown as «چنل ما»
SUPPORT_GROUP = os.getenv("SUPPORT_GROUP_LINK", "")               # full t.me link, shown as «گپ پشتیبانی»
CARD_NUMBER = os.getenv("CARD_NUMBER", "6219861455298157")
CARD_OWNER = os.getenv("CARD_OWNER", "دلیر")
BUY_MIN, BUY_MAX = int(os.getenv("BUY_MIN", "100")), 1_000_000
buy: dict[int, dict] = {}                       # uid -> {"n": "<digits typed so far>"}
pending: dict[int, tuple] = {}                  # uid -> ("code",) | ("receipt", amount)

bot = TelegramClient(StringSession(), API_ID, API_HASH)
login: dict[int, dict] = {}

G, R = "success", "danger"          # Bot API button styles (green / red)


def B(text, data, style=None):
    return botapi.btn(text, data, style)


def esc(x):
    return html.escape(str(x), quote=False)


WELCOME = """🌟 <b>به ربات سلف‌ساز خوش اومدی!</b>

بهترین سلف‌ساز تلگرام؛ اکانتت رو هوشمند کن، بدون هیچ دانش فنی 😍
━━━━━━━━━━
⭐ <b>سلف چیه؟</b>
یه دستیار هوشمند که روی اکانت خودت نصب میشه و دستوراتت رو انجام میده.

🔹 ساعت روی اسم + فونت‌های مختلف
🔹 منشی خودکار و پاسخ کلمه‌ای
🔹 ضد حذف و ادیت + ذخیره‌ی تایمردار
🔹 ترجمه، ارز، هوا، دانلودر و ...
━━━━━━━━━━
🚀 <b>چطوری شروع کنم؟</b>
1️⃣ ربات رو استارت کن
2️⃣ از «خرید الماس» الماس بگیر (یا با دعوت دوستان رایگان جمع کن)
3️⃣ از «مدیریت سلف» سلف رو فعال کن
4️⃣ لذت ببر 🎉

💎 هر ۱۵۰۰ الماس = ۳۰ روز | ورود سلف: {cost} الماس | هدیه‌ی ورود: {welcome} الماس
<blockquote>🛡 نکته امنیتی: کد ورود فقط برای فعال‌سازی سلف داخل همین ربات وارد میشه؛ رمز دومرحله‌ای رو برای هیچ‌کس جز همین ربات نفرست. 🚫</blockquote>"""


def welcome_text():
    t = WELCOME.format(welcome=db.WELCOME_BONUS, cost=db.LOGIN_COST)
    return t


async def left_text(uid):
    if uid in ADMINS:
        return "نامحدود"
    left = await db.expires_at(uid) - time.time()
    if left <= 0:
        return "ندارد" if not await db.user_get(uid) else "منقضی شده"
    return f"{int(left // 86400)} روز و {int(left % 86400 // 3600)} ساعت"


async def bal_text(uid):
    return "نامحدود" if uid in ADMINS else db.fmt(await db.balance(uid))


async def menu_markup(uid):
    rows = [[B("مدیریت سلف", "m:mg", R)],
            [B(f"⟦ {await left_text(uid)} | {await bal_text(uid)} الماس ⟧", "m:status", R)],
            [B("خرید الماس", "m:buy", G), B("حساب کاربری", "m:acc", G)],
            [B("کد الماس", "m:code", R)],
            [B("زیر مجموعه", "m:ref", R),
             botapi.url_btn("پشتیبانی", f"https://t.me/{SUPPORT}", R) if SUPPORT else B("پشتیبانی", "m:sup", R)]]
    last = []
    if CHANNEL:
        last.append(botapi.url_btn("چنل ما", f"https://t.me/{CHANNEL}", R))
    if SUPPORT_GROUP:
        last.append(botapi.url_btn("گپ پشتیبانی", SUPPORT_GROUP, R))
    if last:
        rows.append(last)
    return {"inline_keyboard": rows}


async def send_menu(uid, note=None):
    """(Optional note, then) the main menu: banner + welcome text + colored buttons."""
    if note:
        await bot.send_message(uid, note)
    return await botapi.send_banner(uid, welcome_text(), await menu_markup(uid))


def main_menu():
    """Plain (uncolored) fallback keyboard, only used by the few places that cannot call send_menu."""
    return [[Button.inline("📲 ساخت / ورود سلف", b"m:mg")]]


def rows_to_buttons(rows):
    return botapi.to_telethon({"inline_keyboard": rows})


def need_diamonds_text(bal):
    t = (f"⛔ برای ورود سلف به اکانتت **{db.LOGIN_COST}** الماس لازمه ولی موجودیت **{db.fmt(bal)}** هست.\n\n"
         f"💳 ۱۵۰۰ الماس = {db.price_toman(1500):,} تومان\n"
         "از دکمه‌ی «خرید الماس» استفاده کن یا هدیه‌ی روزانه / کد هدیه / دعوت دوستان رو بزن.")
    return t


def keypad_markup():
    k = lambda t, d, st=R: B(t, d, st)
    rows = [[k(str(n), f"k:{n}") for n in r] for r in ((1, 2, 3), (4, 5, 6), (7, 8, 9))]
    rows.append([k("0", "k:0")])
    rows.append([k("تایید کردن", "k:ok", G), k("پاک کردن", "k:b", R)])
    rows.append([k("ارسال مجدد کد", "k:re", R), k("انصراف", "k:x", R)])
    return {"inline_keyboard": rows}


def keypad_text(st):
    return ("کد به تلگرام شما ارسال شد.\nکد را از طریق پنل شیشه‌ای بنویسید\n"
            f"کد وارد شده : <code>{esc(st['code']) or '—'}</code>")


async def kpad_send(uid, text=None, st=None):
    return await botapi.send_text(uid, text or keypad_text(st), keypad_markup())


async def edit_view(e, text, rows):
    """Edit the message a button was pressed on (chat message or inline panel) with colored buttons."""
    markup = {"inline_keyboard": rows}
    mid = getattr(e.query, "msg_id", None)
    try:
        target = ({"chat_id": e.sender_id, "message_id": mid} if isinstance(mid, int)
                  else {"inline_message_id": botapi.pack_inline_id(mid)})
        await botapi.edit_any(target, text, markup)
    except Exception:
        log.exception("styled edit failed, falling back to plain buttons")
        try:
            await e.edit(text, buttons=botapi.to_telethon(markup), parse_mode="html")
        except errors.MessageNotModifiedError:
            pass


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
            await send_menu(uid, "⌛ زمان ورود تموم شد. دوباره امتحان کن.")
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
        return await send_menu(
            uid, "⛔ این شماره متعلق به همین اکانت تلگرامی که با ربات حرف می‌زنی نیست. "
                 "فقط روی اکانت خودت می‌تونی سلف بسازی.")
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
    await db.uset_flag(uid, "selfon", True)
    await bot.send_message(
        uid, f"✅ **سلف با موفقیت فعال و روشن شد!** ({cost})\nبرای باز کردن پنل بنویس `.پنل`\n\n"
             + await selfbot.status_text(uid))
    await send_menu(uid)


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
        return await kpad_send(uid, "❌ کد اشتباهه. دوباره با دکمه‌ها بزن:\nکد وارد شده : —")
    except errors.PhoneCodeExpiredError:
        st["code"] = ""
        return await kpad_send(uid, "⌛ کد منقضی شده. «ارسال مجدد کد» رو بزن:")
    except errors.FloodWaitError as ex:
        await cancel_login(uid)
        return await send_menu(uid, f"⏳ تلگرام محدودت کرد. {ex.seconds} ثانیه بعد دوباره امتحان کن.")
    except Exception as ex:
        await cancel_login(uid)
        log.exception("sign_in failed")
        return await send_menu(uid, f"⚠️ خطا: {type(ex).__name__}")
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
        await edit_view(e, "❌ ورود لغو شد.", [])
        return await send_menu(uid)
    if x == "re":
        try:
            await send_code(uid, st)
        except errors.FloodWaitError as ex:
            return await e.answer(f"{ex.seconds} ثانیه صبر کن", alert=True)
        await e.answer("کد دوباره ارسال شد 📨")
        return await edit_view(e, keypad_text(st), keypad_markup()["inline_keyboard"])
    if x == "b":
        st["code"] = st["code"][:-1]
    elif x != "ok":
        st["code"] += x
    if x == "ok" or len(st["code"]) >= st["length"]:
        await e.answer()
        if not st["code"]:
            return
        await edit_view(e, keypad_text(st) + "\n\n⏳ در حال ورود...", [])
        return await submit_code(uid)
    await e.answer()
    await edit_view(e, keypad_text(st), keypad_markup()["inline_keyboard"])


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
            return await send_menu(uid)
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
        await e.respond(f"📨 در حال ارسال کد برای `{phone}` ...", buttons=Button.clear())
        return await kpad_send(uid, st=st)

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
            await send_menu(uid, f"⚠️ خطا: {type(ex).__name__}")
            return
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
    await send_menu(uid)
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
            return await e.answer("سلفت همین الان فعاله ✅ (داخل هر چتی .پنل بزن)", alert=True)
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
            uid, "لطفا شماره تلفن خود را از طریق دکمه زیر ارسال کنید:\n(یا خودت تایپ کن؛ هر فرمتی قبوله)",
            buttons=[[Button.request_phone("ارسال شماره تلفن", resize=True, single_use=True)],
                     [Button.text("❌ انصراف", resize=True, single_use=True)]])
    if act == "bal":
        return await e.answer(f"💎 موجودی: {db.fmt(await db.balance(uid))}", alert=True)
    if act == "status":
        return await e.answer(f"⏳ {await left_text(uid)}\n💎 {await bal_text(uid)} الماس", alert=True)
    if act in ("noop",):
        return await e.answer()
    if act == "sup":
        return await e.answer("پشتیبانی هنوز تنظیم نشده.", alert=True)
    if act == "mg":
        await e.answer()
        return await edit_view(e, *await manage_view(uid))
    if act == "acc":
        await e.answer()
        return await edit_view(e, *await account_view(uid, await e.get_sender(), bot_side=True))
    if act == "renew":
        if not await db.user_get(uid):
            return await e.answer("اول سلف بساز", alert=True)
        if not await db.renew(uid):
            return await e.answer(f"الماس کافی نیست ({db.DIAMONDS_PER_30_DAYS} 💎 لازمه)", alert=True)
        await e.answer("✅ ۳۰ روز تمدید شد", alert=True)
        return await edit_view(e, *await manage_view(uid))
    if act == "logout":
        await e.answer()
        return await edit_view(e, "<blockquote>⚠️ مطمئنی؟ سلف حذف میشه و نشست از تلگرام هم خارج میشه.\n"
                                  "برای ورود دوباره دوباره الماس کسر میشه.</blockquote>",
                               [[B("بله، حذف کن", "m:logout2", R)], [B("بازگشت", "m:mg", G)]])
    if act == "logout2":
        await e.answer()
        c = selfbot.clients.get(uid)
        if c:
            try:
                await c.log_out()
            except Exception:
                pass
        await selfbot.stop_user(uid)
        await db.user_remove(uid)
        await bot.send_message(uid, "🚪 سلف حذف شد و نشست از تلگرام هم خارج شد.")
        return await edit_view(e, welcome_text(), (await menu_markup(uid))["inline_keyboard"])
    if act == "help":
        await e.answer()
        text, rows = catalog.view("c0", lambda p: f"h:{p}", "m:menu", await toggle_state(uid))
        return await edit_view(e, text, rows)
    if act == "menu":
        await e.answer()
        return await edit_view(e, welcome_text(), (await menu_markup(uid))["inline_keyboard"])
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


# =========================================================== manage / account / help views
async def manage_view(uid):
    active = bool(selfbot.clients.get(uid)) and await db.user_is_active(uid)
    head = "<blockquote>مدیریت سلف</blockquote>\n"
    if not active:
        return head + "سلفی روی اکانتت فعال نیست. برای ساختش دکمه‌ی زیر رو بزن 👇", [
            [B("فعالسازی سلف", "m:login", G)], [B("بازگشت", "m:menu", R)]]
    return (head + f"✅ سلف فعاله\n⏳ اشتراک: {await left_text(uid)}\n💎 الماس: {await bal_text(uid)}"), [
        [B("دستورات و قابلیت‌ها", "m:help", G)],
        [B("تمدید ۳۰ روزه", "m:renew", G), B("خروج و حذف سلف", "m:logout", R)],
        [B("بازگشت", "m:menu", R)]]


async def info_rows(uid, user):
    name = utils.clean_name(getattr(user, "first_name", "") or "") or "-"
    uname = f"@{user.username}" if getattr(user, "username", None) else "-"
    pairs = [("اسم", name[:24]), ("آیدی عددی", str(uid)), ("یوزرنیم", uname),
             ("موجودی الماس", await bal_text(uid)), ("انقضا", await left_text(uid))]
    rows = [[B(v, "m:noop", G), B(l, "m:noop", R)] for l, v in pairs]
    rows.append([botapi.url_btn("سلف ساز ما", f"https://t.me/{BOT_USERNAME}", G)])
    return rows


async def account_view(uid, user, bot_side=False, pre=None):
    rows = await info_rows(uid, user)
    if bot_side:
        rows += [[B("هدیه روزانه", "m:daily", G), B("تراکنش‌ها", "m:hist", G)],
                 [B("دستورات و راهنما", "m:help", G), B("تمدید ۳۰ روزه", "m:renew", G)],
                 [B("بازگشت", "m:menu", R)]]
    else:
        rows += [[B("تمدید ۳۰ روزه", f"p:{uid}:renew", G)], [B("بازگشت", f"p:{uid}:main", R)]]
    return "<blockquote>Your Account | اکانت شما 👤</blockquote>", rows


async def toggle_state(uid):
    return {cmd: await db.uflag(uid, key, default) for cmd, (key, default) in catalog.TOGGLE_OF.items()}


async def apply_toggle(uid, path):
    """path 't<i>:<j>' -> flips that feature's switch. Returns the detail path to show, or None if not possible."""
    i, _, j = path[1:].partition(":")
    cmd = catalog.CATS[int(i)][1][int(j)][0]
    key, default = catalog.TOGGLE_OF[cmd]
    if not await db.user_get(uid):
        return None
    new = not await db.uflag(uid, key, default)
    if key == "clock":
        await selfbot.set_clock(uid, new)
    else:
        await db.uset_flag(uid, key, new)
    return f"f{i}:{j}"


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
    uid, path = e.sender_id, e.data.decode()[2:]
    if path.startswith("r"):
        await e.answer()
        return await run_action(uid, path[1:])
    if path.startswith("t"):
        path = await apply_toggle(uid, path)
        if path is None:
            return await e.answer("اول سلف بساز", alert=True)
    text, rows = catalog.view(path, lambda p: f"h:{p}", "m:menu", await toggle_state(uid))
    await e.answer()
    await edit_view(e, text, rows)


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
        await e.edit("❌ خرید لغو شد.", buttons=None)
        return await send_menu(uid)
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


# =========================================================== inline panel (opened with .پنل)
async def panel_main(uid):
    active = await db.user_is_active(uid)
    power = await db.uflag(uid, "selfon", True)
    state = "⛔ منقضی" if not active else ("🟢 روشن" if power else "⏸ خاموش")
    text = (f"<blockquote>Panel ..</blockquote>\nوضعیت سلف: {state}\n"
            f"⏳ {await left_text(uid)}  |  💎 {await bal_text(uid)}\n🕒 {selfbot.now_fa()}")
    rows = [[B("مدیریت سلف", f"p:{uid}:cmds", R), B("حساب کاربری", f"p:{uid}:acc", R)],
            [B("بستن پنل", f"p:{uid}:close", R)]]
    return text, rows


@bot.on(events.InlineQuery)
async def on_inline(e):
    uid = e.sender_id
    if e.text.strip() not in ("panel", "پنل") or not await db.user_get(uid):
        return
    text, rows = await panel_main(uid)
    markup = {"inline_keyboard": rows}
    pid = await botapi.photo_id(uid)
    if pid:
        res = {"type": "photo", "id": "panel", "photo_file_id": pid, "caption": text,
               "parse_mode": "HTML", "reply_markup": markup}
    else:
        res = {"type": "article", "id": "panel", "title": "🛠 پنل سلف", "reply_markup": markup,
               "input_message_content": {"message_text": text, "parse_mode": "HTML"}}
    await botapi.call("answerInlineQuery", {"inline_query_id": e.id, "results": [res],
                                            "cache_time": 0, "is_personal": True})


@bot.on(events.CallbackQuery(pattern=rb"^p:"))
async def cb_panel(e):
    _, uid_s, act = e.data.decode().split(":", 2)
    uid = int(uid_s)
    if e.sender_id != uid:
        return await e.answer("این پنل برای تو نیست 😉", alert=True)
    mk = lambda p: f"p:{uid}:H{p}"
    back = f"p:{uid}:main"
    if act == "close":
        await e.answer()
        return await edit_view(e, "پنل بسته شد 🔒", [])
    if act == "renew":
        if not await db.renew(uid):
            return await e.answer(f"الماس کافی نیست ({db.DIAMONDS_PER_30_DAYS} 💎 لازمه)", alert=True)
        await e.answer("✅ ۳۰ روز تمدید شد", alert=True)
        act = "acc"
    if act == "acc":
        await e.answer()
        return await edit_view(e, *await account_view(uid, await e.get_sender()))
    if act == "cmds":
        await e.answer()
        text, rows = catalog.view("c0", mk, back, await toggle_state(uid))
        return await edit_view(e, text, rows)
    if act.startswith("H"):
        path = act[1:]
        if path.startswith("r"):
            await e.answer()
            return await run_action(uid, path[1:])
        if path.startswith("t"):
            path = await apply_toggle(uid, path)
            if path is None:
                return await e.answer("اول سلف بساز", alert=True)
        text, rows = catalog.view(path, mk, back, await toggle_state(uid))
        await e.answer()
        return await edit_view(e, text, rows)
    await e.answer()
    await edit_view(e, *await panel_main(uid))


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
                            uid, "⛔ اشتراک سلفت تموم شد. با ۱۵۰۰ 💎 تمدید کن (از «مدیریت سلف» یا `.تمدید`).")
                        await send_menu(uid)
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
                await send_menu(uid, "⚠️ نشست سلفت دیگه معتبر نیست (از تلگرام خارج شدی). دوباره وارد شو.")
        except Exception:
            log.exception("failed to start self for %s", uid)
    asyncio.create_task(expiry_loop())
    log.info("running: %s selfs", len(selfbot.clients))
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
