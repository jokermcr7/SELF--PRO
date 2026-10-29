"""Manager bot: onboarding (phone + keypad code login), panel, wallet, admin tools, game group."""
import asyncio
import html
import logging
import os
import re
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
pending: dict[int, tuple] = {}                  # uid -> ("code",) | ("receipt", amount) | ("gcode",) | ("gamount",) | ("gusers",)
gift: dict[int, dict] = {}                      # admin gift-code builder: uid -> {"code", "amount", "uses"}
USERS_PER_PAGE = 8

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

🔹 ساعت زنده روی اسم/فامیل/بیو + ۱۲ فونت
🔹 ایموجی پریمیوم خودکار، اسپم، قفل پیوی و ...
🔹 منشی خودکار و پاسخ کلمه‌ای
🔹 ضد حذف و ادیت + ذخیره‌ی تایمردار
🔹 ترجمه، ارز، هوا، دانلودر و ...
━━━━━━━━━━
🚀 <b>چطوری شروع کنم؟</b>
1️⃣ ربات رو استارت کن
2️⃣ از «خرید الماس» الماس بگیر (یا با دعوت دوستان رایگان جمع کن)
3️⃣ از «مدیریت سلف» سلف رو فعال کن
4️⃣ لذت ببر 🎉

💎 هر {pack} الماس = ۳۰ روز | مصرف ساعتی: {rate} الماس (تا وقتی سلف روی اکانته) | هدیه‌ی ورود: {welcome} الماس
<blockquote>🛡 نکته امنیتی: کد ورود فقط برای فعال‌سازی سلف داخل همین ربات وارد میشه؛ رمز دومرحله‌ای رو برای هیچ‌کس جز همین ربات نفرست. 🚫
ℹ️ اطلاعات اکانتِ سلف‌های فعال (مثل شماره و وضعیت) فقط برای مدیر ربات قابل مشاهده‌ست.</blockquote>"""


def welcome_text():
    return WELCOME.format(welcome=db.WELCOME_BONUS, pack=f"{db.DIAMONDS_PER_30_DAYS:,}", rate=f"{db.RATE_PER_HOUR:.2f}")


async def left_text(uid):
    """Exact time the user's diamonds are worth: X روز و Y ساعت و Z دقیقه."""
    return await db.time_left_text(uid)


async def bal_text(uid):
    return "نامحدود" if uid in ADMINS else db.fmt(await db.balance(uid))


async def menu_markup(uid):
    rows = [[B("مدیریت سلف", "m:mg", R)],
            [B(f"⟦ {await db.time_left_short(uid)} | {await bal_text(uid)} الماس ⟧", "m:status", R)],
            [B("خرید الماس", "m:buy", G), B("حساب کاربری", "m:acc", G)],
            [B("کد هدیه", "m:code", R)],
            [B("زیر مجموعه", "m:ref", R),
             botapi.url_btn("پشتیبانی", f"https://t.me/{SUPPORT}", R) if SUPPORT else B("پشتیبانی", "m:sup", R)]]
    if uid in ADMINS:                                   # owner-only tools
        rows.insert(3, [B("🎟 ساخت کد هدیه", "a:gift", G), B("👥 تمامی کاربران", "a:users:0", G)])
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
    return (f"⛔ برای فعال شدن سلف حداقل برای **۱ ساعت** الماس لازمه (≈ {db.RATE_PER_HOUR:.2f} 💎 در ساعت) "
            f"ولی موجودیت **{db.fmt(bal)}** هست.\n\n"
            f"💳 ۱۵۰۰ الماس = {db.price_toman(1500):,} تومان ({db.DIAMONDS_PER_30_DAYS:,} الماس = ۳۰ روز سلف)\n"
            "از دکمه‌ی «خرید الماس» استفاده کن یا کد هدیه / دعوت دوستان رو بزن.")


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
    # hourly billing: the self needs at least one hour of diamonds to start (owner is free)
    if not await db.has_funds(uid):
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
        return await bot.send_message(uid, "⚠️ ورود انجام شد ولی سلف بالا نیومد. دوباره امتحان کن (الماسی کم نشد).")
    await db.uset_flag(uid, "selfon", True)
    await db.uset(uid, "exp_notified", "0")
    billing_reset(uid)
    cost = "رایگان (مالک)" if db.is_admin(uid) else f"از الان ساعتی ≈ {db.RATE_PER_HOUR:.2f} 💎 مصرف میشه"
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


@bot.on(events.NewMessage(pattern=r"^/(bal|code|buy|history)(@\w+)?(?:\s+(\S+))?$", func=lambda e: e.is_private))
async def on_cmds(e):
    uid, c = e.sender_id, e.pattern_match.group(1)
    arg = e.pattern_match.group(3)
    if c == "bal" and arg and uid in ADMINS:
        return                           # admin form "/bal <id>" is handled separately
    await db.ensure_wallet(uid, utils.clean_name(getattr(await e.get_sender(), "first_name", "")))
    if c == "bal":
        bal = await db.balance(uid)
        text, markup = botapi.balance_card(bal, db.fmt(bal).replace("♾ ", "").replace("نامحدود", "∞"), db.value_text(bal))
        try:
            return await botapi.send_text(uid, text, markup, reply_to=e.id)
        except Exception:
            return await e.respond(f"💎 موجودی: **{db.fmt(bal)}**\n💰 {db.value_text(bal)}")
    if c == "code":
        if not arg:
            pending[uid] = ("code",)
            return await e.respond("🎟 کد هدیه‌ات رو بفرست:")
        return await e.respond(await do_code(uid, arg))
    if c == "buy":
        return await start_buy(uid)
    if c == "history":
        return await e.respond(await hist_text(uid))


async def do_code(uid, code):
    r = await db.code_redeem(uid, code.strip())
    if r == "none":
        return "❌ این کد وجود نداره."
    if r == "used":
        return "⚠️ این کد رو قبلاً استفاده کردی."
    if r == "full":
        return "⌛ ظرفیت این کد تموم شده."
    return f"🎉 کد فعال شد! +{r:,} 💎\n💎 موجودی: {db.fmt(await db.balance(uid))}"


REASONS = {"welcome": "🎁 هدیه ورود", "referral": "👥 دعوت", "admin": "👑 ادمین", "code": "🎟 کد هدیه",
           "self_hourly": "⏱ مصرف ساعتی سلف", "self_login": "📲 ورود سلف", "purchase": "💳 خرید", "transfer_in": "📥 دریافت",
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
        if not await db.has_funds(uid):
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
        bal = await db.balance(uid)
        return await e.answer(f"💎 موجودی: {db.fmt(bal)}\n💰 ارزش: {db.value_text(bal)}", alert=True)
    if act == "status":
        extra = "" if uid in ADMINS else f"\n📉 {db.RATE_PER_HOUR:.2f} 💎 در ساعت | {db.DIAMONDS_PER_30_DAYS:,} = ۳۰ روز"
        return await e.answer(f"⏳ {await left_text(uid)}\n💎 {await bal_text(uid)} الماس{extra}", alert=True)
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
        text, rows = catalog.view("", lambda p: f"h:{p}", "m:menu", await toggle_state(uid))
        return await edit_view(e, text, rows)
    if act == "menu":
        await e.answer()
        return await edit_view(e, welcome_text(), (await menu_markup(uid))["inline_keyboard"])
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
    if not selfbot.clients.get(uid):
        return head + "سلفی روی اکانتت فعال نیست. برای ساختش دکمه‌ی زیر رو بزن 👇", [
            [B("فعالسازی سلف", "m:login", G)], [B("بازگشت", "m:menu", R)]]
    state = "✅ سلف فعاله" if active else "⛔ الماس تموم شده؛ سلف متوقفه (با خرید الماس یا کد هدیه خودکار ادامه میده)"
    if active and not await db.uflag(uid, "selfon", True):
        state = "⏸ سلف خاموشه (ولی هزینه‌ی ساعتی همچنان کسر میشه)"
    rate = "" if uid in ADMINS else f"\n📉 مصرف: {db.RATE_PER_HOUR:.2f} 💎 در ساعت"
    return (head + f"{state}\n⏳ زمان باقی‌مانده: {await left_text(uid)}\n💎 الماس: {await bal_text(uid)}{rate}"), [
        [B("دستورات و قابلیت‌ها", "m:help", G)],
        [B("خرید الماس", "m:buy", G), B("خروج و حذف سلف", "m:logout", R)],
        [B("بازگشت", "m:menu", R)]]


async def info_rows(uid, user):
    name = utils.clean_name(getattr(user, "first_name", "") or "") or "-"
    uname = f"@{user.username}" if getattr(user, "username", None) else "-"
    pairs = [("اسم", name[:24]), ("آیدی عددی", str(uid)), ("یوزرنیم", uname),
             ("موجودی الماس", await bal_text(uid)), ("زمان باقی‌مانده", await db.time_left_short(uid))]
    rows = [[B(v, "m:noop", G), B(l, "m:noop", R)] for l, v in pairs]
    rows.append([botapi.url_btn("سلف ساز ما", f"https://t.me/{BOT_USERNAME}", G)])
    return rows


async def account_view(uid, user, bot_side=False, pre=None):
    rows = await info_rows(uid, user)
    if bot_side:
        rows += [[B("خرید الماس", "m:buy", G), B("تراکنش‌ها", "m:hist", G)],
                 [B("دستورات و راهنما", "m:help", G)],
                 [B("بازگشت", "m:menu", R)]]
    else:
        rows += [[B("بازگشت", f"p:{uid}:main", R)]]
    left = await left_text(uid)
    extra = "" if uid in ADMINS else (f"\n⏳ {left}\n📉 {db.RATE_PER_HOUR:.2f} 💎 در ساعت "
                                      f"({db.RATE_PER_DAY:.1f} در روز) | {db.DIAMONDS_PER_30_DAYS:,} الماس = ۳۰ روز")
    return f"<blockquote>Your Account | اکانت شما 👤</blockquote>{extra}", rows


toggle_note: dict[int, str] = {}


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
    if new and key in ("ttl", "antidel"):          # backups go to their own private group
        ok = await selfbot.ensure_backup(uid)
        toggle_note[uid] = (f"📦 گپ «{selfbot.BACKUP_TITLE}» آماده شد" if ok
                            else "⚠️ ساخت گپ بکاپ نشد؛ فعلاً توی Saved Messages ذخیره میشه")
    return f"f{i}:{j}"


async def run_action(uid, key):
    """Execute a feature straight from its help button."""
    if key == "status":
        txt = await selfbot.status_text(uid)
    elif key == "bal":
        txt = f"💎 موجودی: **{db.fmt(await db.balance(uid))}**"
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


async def style_view(uid, path, mk, back_path):
    """🎨 auto style section: pick styles (combinable) + one font; every outgoing message is edited to match."""
    if not await db.user_get(uid):
        return None
    if path.startswith("st:"):
        if path[3:] in selfbot.STYLE_ORDER:
            await selfbot.toggle_mode(uid, path[3:])
    elif path.startswith("sf:"):
        v, cur = path[3:], await selfbot.get_autofont(uid)
        await db.uset(uid, "autofont", "" if (v == cur or v not in utils.FONTS) else v)
    elif path == "s0":
        await db.uset(uid, "textmode", "")
        await db.uset(uid, "autofont", "")
    modes, font = await selfbot.get_modes(uid), await selfbot.get_autofont(uid)
    rows, line = [], []
    for name in selfbot.STYLE_ORDER:
        on = name in modes
        line.append(B(f"{selfbot.STYLE_LABEL[name]} ⟨{'✓' if on else '✗'}⟩", mk(f"st:{name}"), G if on else R))
        if len(line) == 2:
            rows.append(line)
            line = []
    line = []
    for k, f in utils.FONTS.items():
        line.append(B(f"{k}· {f('Abc')}{' ✓' if font == k else ''}", mk(f"sf:{k}"), G if font == k else R))
        if len(line) == 3:
            rows.append(line)
            line = []
    rows.append([B("🔴 خاموش کردن همه", mk("s0"), R)])
    rows.append([B("بازگشت به دستورات", mk(back_path), R)])
    if modes or font:
        now = " + ".join(selfbot.STYLE_LABEL[m] for m in modes) or "—"
        state = f"🟢 <b>روشن</b>\nسبک: {esc(now)}\nفونت: {esc(utils.FONTS[font]('Hello')) if font else '—'}"
    else:
        state = "🔴 <b>خاموش</b>"
    text = ("<b>🎨 حالت خودکار متن</b>\n<blockquote>هر پیامی که بفرستی خودکار با سبک و فونت انتخابی ارسال (ویرایش) میشه. "
            "سبک{0}ها رو می{0}تونی ترکیب کنی؛ «مونو» با بقیه ترکیب نمیشه. فونت فقط روی حروف انگلیسی اثر داره "
            "و لینک، @منشن و دستورها دست نمی{0}خورن.</blockquote>\n".format("\u200c") + state +
            "\nروی هر دکمه بزنی روشن/خاموش میشه 👇")
    return text, rows


async def clock_view(uid, path, mk, back_path):
    """⏰ clock section: targets (name / family / bio) + font picker. Same look as the reference video."""
    c = selfbot.clients.get(uid)
    if path.startswith("kt:") or path.startswith("kp:"):
        if not c:
            return None
        if path.startswith("kt:"):
            tgt = path[3:]
            if tgt in selfbot.CLOCK_TARGETS:
                fl = await selfbot.clock_flags(uid)
                await selfbot.set_clock_target(uid, tgt, not fl[tgt])
            path = "k"
        else:
            v = path[3:]
            await db.uset(uid, "clock_font", v if v == "rand" else int(v))
            await selfbot.push_clock(c, uid)
            path = "kf"
    if path == "kf":
        cur = await db.uget(uid, "clock_font", "0")
        hm = selfbot.datetime.now(selfbot.TZ).strftime("%H:%M")
        rows, line = [], []
        for i in range(len(selfbot.DIGIT_FONTS)):
            on = cur == str(i)
            line.append(B(f"{selfbot.clock_str(hm, i)} ⟨{'✓' if on else '✗'}⟩", mk(f"kp:{i}"), G if on else R))
            if len(line) == 2:
                rows.append(line)
                line = []
        rows.append([B(f"رندوم ⟨{'✓' if cur == 'rand' else '✗'}⟩", mk("kp:rand"), G if cur == "rand" else R)])
        rows.append([B("بازگشت", mk("k"), R)])
        return "<blockquote>یکی از فونت‌های زیر رو انتخاب کن؛ همون لحظه اعمال میشه.</blockquote>", rows
    fl = await selfbot.clock_flags(uid)
    sw = lambda n, t: B(f"{n} ⟨{'✓' if fl[t] else '✗'}⟩", mk(f"kt:{t}"), G if fl[t] else R)
    rows = [[sw("فامیل", "last"), sw("اسم", "name")], [sw("بیو", "bio"), B("فونت‌ها", mk("kf"), R)],
            [B("بازگشت به دستورات", mk(back_path), R)]]
    return ("<b>بخش ساعت ⏰</b>\n<blockquote>ساعت زنده توی اسم، فامیل و بیو؛ دقیقاً لحظه‌ی عوض شدن دقیقه آپدیت میشه. "
            "فونت دلخواهت رو هم انتخاب کن.</blockquote>"), rows


@bot.on(events.CallbackQuery(pattern=rb"^h:"))
async def cb_help(e):
    uid, path = e.sender_id, e.data.decode()[2:]
    if path.startswith("r"):
        await e.answer()
        return await run_action(uid, path[1:])
    if path.startswith("k"):
        ci, cj = catalog.find("ساعت")
        res = await clock_view(uid, path, lambda p: f"h:{p}", f"f{ci}:{cj}")
        if res is None:
            return await e.answer("اول سلف بساز", alert=True)
        await e.answer()
        return await edit_view(e, *res)
    if path.startswith("s"):
        ci, cj = catalog.find("حالت")
        res = await style_view(uid, path, lambda p: f"h:{p}", f"f{ci}:{cj}")
        if res is None:
            return await e.answer("اول سلف بساز", alert=True)
        await e.answer()
        return await edit_view(e, *res)
    if path.startswith("t"):
        path = await apply_toggle(uid, path)
        if path is None:
            return await e.answer("اول سلف بساز", alert=True)
    text, rows = catalog.view(path, lambda p: f"h:{p}", "m:menu", await toggle_state(uid))
    await e.answer(toggle_note.pop(uid, None))
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
    if kind[0] in ("gcode", "gamount", "gusers"):
        if uid not in ADMINS:
            pending.pop(uid, None)
            return
        return await gift_input(e, uid, kind[0], t)
    if kind[0] in ("uadd", "utake", "usearch"):
        if uid not in ADMINS:
            pending.pop(uid, None)
            return
        n = utils.to_en_digits(t).strip()
        if not n.isdigit() or int(n) <= 0:
            return await e.respond("⚠️ یه عدد مثبت بفرست (یا /start برای لغو):")
        pending.pop(uid, None)
        n = int(n)
        if kind[0] == "usearch":
            if not (await db.user_full(n))["wallet"]:
                return await e.respond("⚠️ همچین کاربری توی ربات پیدا نشد.")
            text, rows = await user_detail_view(n, 0)
            return await botapi.send_text(uid, text, {"inline_keyboard": rows})
        tuid, page = kind[1], kind[2]
        if kind[0] == "uadd":
            bal = await db.add(tuid, n, "admin")
            msg = f"✅ {n:,} 💎 به `{tuid}` اضافه شد."
            try:
                await bot.send_message(tuid, f"💎 {n:,} الماس از طرف مدیر به حسابت اضافه شد. موجودی: {db.fmt(bal)}")
            except Exception:
                pass
        else:
            have = await db.balance(tuid)
            take = min(n, have)
            if take <= 0 or await db.try_spend(tuid, take, "admin_take") is None:
                return await e.respond("⚠️ این کاربر الماسی نداره.")
            msg = f"✅ {take:,} 💎 از `{tuid}` کم شد." + (" (موجودی‌اش کمتر از مقدار بود؛ همه‌اش کم شد)" if take < n else "")
        await e.respond(msg)
        text, rows = await user_detail_view(tuid, page)
        return await botapi.send_text(uid, text, {"inline_keyboard": rows})
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
    state = "⛔ الماس تموم شده" if not active else ("🟢 روشن" if power else "⏸ خاموش")
    text = (f"<blockquote>Panel ..</blockquote>\nوضعیت سلف: {state}\n"
            f"⏳ {await left_text(uid)}  |  💎 {await bal_text(uid)}\n🕒 {selfbot.now_fa()}")
    rows = [[B("مدیریت سلف", f"p:{uid}:cmds", R), B("حساب کاربری", f"p:{uid}:acc", R)],
            [B("بستن پنل", f"p:{uid}:close", R)]]
    return text, rows


@bot.on(events.InlineQuery)
async def on_inline(e):
    uid = e.sender_id
    q = e.text.strip()
    if q.startswith("emo:"):                       # self -> premium emoji message sent through the bot
        p = selfbot.INLINE_PAYLOADS.get(q[4:])
        if not p or p["uid"] != uid:
            return
        res = {"type": "article", "id": q[4:], "title": "⭐ ایموجی پریمیوم", "description": p["text"][:60],
               "input_message_content": {"message_text": p["text"], "entities": p["entities"]}}
        try:
            await botapi.call("answerInlineQuery", {"inline_query_id": e.id, "results": [res],
                                                    "cache_time": 0, "is_personal": True})
        except Exception as ex:                    # e.g. bot has no Fragment username -> no result -> self falls back
            log.warning("premium emoji inline rejected: %s", ex)
        return
    if q not in ("panel", "پنل") or not await db.user_get(uid):
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
    if act == "acc":
        await e.answer()
        return await edit_view(e, *await account_view(uid, await e.get_sender()))
    if act == "cmds":
        await e.answer()
        text, rows = catalog.view("", mk, back, await toggle_state(uid))
        return await edit_view(e, text, rows)
    if act.startswith("H"):
        path = act[1:]
        if path.startswith("r"):
            await e.answer()
            return await run_action(uid, path[1:])
        if path.startswith("k"):
            ci, cj = catalog.find("ساعت")
            res = await clock_view(uid, path, mk, f"f{ci}:{cj}")
            if res is None:
                return await e.answer("اول سلف بساز", alert=True)
            await e.answer()
            return await edit_view(e, *res)
        if path.startswith("s"):
            ci, cj = catalog.find("حالت")
            res = await style_view(uid, path, mk, f"f{ci}:{cj}")
            if res is None:
                return await e.answer("اول سلف بساز", alert=True)
            await e.answer()
            return await edit_view(e, *res)
        if path.startswith("t"):
            path = await apply_toggle(uid, path)
            if path is None:
                return await e.answer("اول سلف بساز", alert=True)
        text, rows = catalog.view(path, mk, back, await toggle_state(uid))
        await e.answer(toggle_note.pop(uid, None))
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
    await e.respond(f"👥 کل کاربران ربات: {total}\n📲 سلف ساخته‌شده: {active}\n🟢 سلف در حال اجرا: {len(selfbot.clients)}\n"
                    "لیست کامل: منوی ربات ← «👥 تمامی کاربران»")



# =========================================================== admin only: gift-code builder + all users
def _need_admin(e):
    return e.sender_id in ADMINS


def gift_view(uid):
    g = gift.setdefault(uid, {})
    code = f"<code>{esc(g['code'])}</code>" if g.get("code") else "— تنظیم نشده"
    amount = f"<b>{g['amount']:,}</b> 💎" if g.get("amount") else "— تنظیم نشده"
    uses = f"<b>{g['uses']:,}</b> نفر" if g.get("uses") else "— تنظیم نشده"
    ready = all(g.get(k) for k in ("code", "amount", "uses"))
    text = ("<blockquote>🎟 ساخت کد هدیه | فقط مدیر</blockquote>\n"
            f"🔤 کد: {code}\n💎 تعداد الماس هر نفر: {amount}\n👥 تعداد کاربران مجاز: {uses}\n\n"
            "با دکمه‌های زیر هر بخش رو تنظیم کن، بعد «ساخت کد» رو بزن 👇")
    rows = [[B("🔤 تنظیم کد", "a:gc", G)],
            [B("💎 تعداد الماس", "a:ga", G), B("👥 تعداد کاربر", "a:gu", G)],
            [B("✅ ساخت کد" if ready else "✅ ساخت کد (ناقصه)", "a:gmk", G if ready else R)],
            [B("📋 لیست کدها", "a:gl", R), B("🧹 پاک کردن فرم", "a:gx", R)],
            [B("بازگشت", "m:menu", R)]]
    return text, rows


async def gift_input(e, uid, kind, t):
    g = gift.setdefault(uid, {})
    t = utils.to_en_digits(t).strip()
    if kind == "gcode":
        if not re.fullmatch(r"[\w\-]{3,32}", t):
            return await e.respond("⚠️ کد باید ۳ تا ۳۲ حرف/عدد باشه (بدون فاصله). دوباره بفرست:")
        g["code"] = t.lower()
    elif kind == "gamount":
        if not t.isdigit() or not 1 <= int(t) <= 10_000_000:
            return await e.respond("⚠️ یه عدد بین ۱ تا ۱۰,۰۰۰,۰۰۰ بفرست:")
        g["amount"] = int(t)
    else:
        if not t.isdigit() or not 1 <= int(t) <= 1_000_000:
            return await e.respond("⚠️ یه عدد بین ۱ تا ۱,۰۰۰,۰۰۰ بفرست:")
        g["uses"] = int(t)
    pending.pop(uid, None)
    text, rows = gift_view(uid)
    await botapi.send_text(uid, text, {"inline_keyboard": rows})


async def codes_view():
    rows_db = await db.code_list(8)
    lines = [f"• <code>{esc(r['code'])}</code> — {r['amount']:,} 💎 — {r['used']}/{r['max_uses']} نفر" for r in rows_db]
    text = "<blockquote>📋 کدهای هدیه</blockquote>\n" + ("\n".join(lines) or "هنوز کدی نساختی.")
    rows = [[B(f"🗑 {r['code']}", f"a:gd:{r['code']}"[:60], R)] for r in rows_db]
    rows.append([B("بازگشت", "a:gift", G)])
    return text, rows


@bot.on(events.CallbackQuery(pattern=rb"^a:", func=_need_admin))
async def cb_admin(e):
    uid = e.sender_id
    parts = e.data.decode().split(":")
    act = parts[1]
    if act == "gift":
        pending.pop(uid, None)
        await e.answer()
        return await edit_view(e, *gift_view(uid))
    if act in ("gc", "ga", "gu"):
        kind, ask = {"gc": ("gcode", "🔤 کد دلخواهت رو بفرست (مثلاً `eid1405` — حرف و عدد، ۳ تا ۳۲ کاراکتر):"),
                     "ga": ("gamount", "💎 هر نفر چند الماس بگیره؟ یه عدد بفرست:"),
                     "gu": ("gusers", "👥 چند کاربر می‌تونن از این کد استفاده کنن؟ یه عدد بفرست:")}[act]
        pending[uid] = (kind,)
        await e.answer()
        return await bot.send_message(uid, ask)
    if act == "gx":
        gift[uid] = {}
        await e.answer("پاک شد")
        return await edit_view(e, *gift_view(uid))
    if act == "gmk":
        g = gift.get(uid, {})
        if not all(g.get(k) for k in ("code", "amount", "uses")):
            return await e.answer("اول کد، تعداد الماس و تعداد کاربر رو تنظیم کن", alert=True)
        await db.code_create(g["code"], g["amount"], g["uses"])
        gift[uid] = {}
        await e.answer("✅ کد ساخته شد", alert=True)
        return await edit_view(
            e, f"✅ <b>کد هدیه ساخته شد</b>\n\n🔤 کد: <code>{esc(g['code'])}</code>\n💎 هر نفر: {g['amount']:,}\n"
               f"👥 ظرفیت: {g['uses']:,} نفر\n\nکاربر باید توی ربات «کد هدیه» رو بزنه و این کد رو بفرسته.",
            [[B("🎟 ساخت کد دیگه", "a:gift", G), B("📋 لیست کدها", "a:gl", R)], [B("بازگشت", "m:menu", R)]])
    if act == "gl":
        await e.answer()
        return await edit_view(e, *await codes_view())
    if act == "gd":
        ok = await db.code_delete(":".join(parts[2:]))
        await e.answer("🗑 حذف شد" if ok else "پیدا نشد")
        return await edit_view(e, *await codes_view())
    if act in ("uadd", "utake"):
        tuid, page = int(parts[2]), int(parts[3])
        if tuid in ADMINS:
            return await e.answer("مدیر الماس نامحدود داره", alert=True)
        pending[uid] = (act, tuid, page)
        await e.answer()
        what = "اضافه" if act == "uadd" else "کم"
        return await bot.send_message(uid, f"💎 چند الماس {what} بشه؟ فقط عدد بفرست (برای کاربر `{tuid}`):\nلغو: /start")
    if act == "usearch":
        pending[uid] = ("usearch",)
        await e.answer()
        return await bot.send_message(uid, "🔎 آیدی عددی کاربر رو بفرست:\nلغو: /start")
    if act == "kick":
        tuid, page = int(parts[2]), int(parts[3])
        await e.answer()
        return await edit_view(
            e, f"<blockquote>🥾 کیک از سلف</blockquote>\nسلف کاربر <code>{tuid}</code> متوقف و نشستش از تلگرام خارج میشه. "
               "الماس‌هاش دست‌نخورده می‌مونه و کاربر باید دوباره وارد بشه (۳۰ 💎 هزینه‌ی ورود).\nمطمئنی؟",
            [[B("✅ بله، کیک کن", f"a:kick2:{tuid}:{page}", R)], [B("بازگشت", f"a:u:{tuid}:{page}", G)]])
    if act == "kick2":
        tuid, page = int(parts[2]), int(parts[3])
        c = selfbot.clients.get(tuid)
        if c:
            try:
                await c.log_out()
            except Exception:
                pass
        await selfbot.stop_user(tuid)
        await db.user_remove(tuid)
        tick_ts.pop(tuid, None)
        await e.answer("🥾 کیک شد", alert=True)
        await notify(tuid, "⛔ سلف تو توسط مدیر متوقف شد. برای اطلاعات بیشتر با پشتیبانی در تماس باش.")
        return await edit_view(e, *await user_detail_view(tuid, page))
    if act == "users":
        await e.answer()
        return await edit_view(e, *await users_view(int(parts[2])))
    if act == "u":
        await e.answer()
        return await edit_view(e, *await user_detail_view(int(parts[2]), int(parts[3]) if len(parts) > 3 else 0))


async def users_view(page):
    total = await db.all_users_count()
    pages = max(-(-total // USERS_PER_PAGE), 1)
    page = min(max(page, 0), pages - 1)
    rows_db = await db.all_users(page * USERS_PER_PAGE, USERS_PER_PAGE)
    running = len(selfbot.clients)
    text = (f"<blockquote>👥 تمامی کاربران | فقط مدیر</blockquote>\n"
            f"کل: <b>{total:,}</b> | سلف روشن: <b>{running}</b> | صفحه {page + 1}/{pages}\n"
            "🟢 = سلف فعال دارد (اطلاعات کامل) | ⚪ = فقط ربات رو استارت کرده\nیکی رو انتخاب کن 👇")
    rows = []
    for r in rows_db:
        mark = "🟢" if r["has_self"] else "⚪"
        nm = utils.clean_name(r["name"] or "کاربر")[:14]
        rows.append([B(f"{mark} {nm} · {r['user_id']}", f"a:u:{r['user_id']}:{page}", G if r["has_self"] else R)])
    nav = []
    if page > 0:
        nav.append(B("◀️ قبلی", f"a:users:{page - 1}", R))
    if page < pages - 1:
        nav.append(B("بعدی ▶️", f"a:users:{page + 1}", R))
    if nav:
        rows.append(nav)
    rows.append([B("🔎 جستجو با آیدی", "a:usearch", G)])
    rows.append([B("بازگشت", "m:menu", R)])
    return text, rows


async def user_detail_view(tuid, page):
    f = await db.user_full(tuid)
    w, u = f["wallet"], f["user"]
    st = f["settings"]
    flag = lambda k, d=False: ("🟢" if (st.get(k, "1" if d else "0") == "1") else "🔴")
    name = esc(utils.clean_name((w and w["name"]) or "کاربر"))
    head = f"<blockquote>👤 {name}</blockquote>\n🆔 <code>{tuid}</code>\n"
    back = [B("🔄 بروزرسانی", f"a:u:{tuid}:{page}", G), B("◀️ لیست", f"a:users:{page}", R)]
    has_self = bool(u and u["active"] and u["has_session"])
    rows = [[B("➕ اضافه کردن الماس", f"a:uadd:{tuid}:{page}", G), B("➖ کم کردن الماس", f"a:utake:{tuid}:{page}", R)]]
    if has_self and tuid not in ADMINS:
        rows.append([B("🥾 کیک از سلف", f"a:kick:{tuid}:{page}", R)])
    rows += [back, [B("بازگشت به منو", "m:menu", R)]]
    bal = await bal_text(tuid)
    if not has_self:
        txt = (head + f"💎 الماس: {bal}\n👥 دعوت‌شده‌ها: {f['invited']}\n"
               "⚪ سلف فعال نکرده؛ اطلاعات کامل فقط برای کاربران دارای سلف فعال نمایش داده میشه.")
        return txt, rows
    live = ""
    c = selfbot.clients.get(tuid)
    if c:
        try:
            me = await c.get_me()
            uname = f"@{me.username}" if me.username else "—"
            full = esc(((me.first_name or "") + " " + (me.last_name or "")).strip() or "—")
            live = (f"📛 اسم فعلی: {full}\n🔗 یوزرنیم: {esc(uname)}\n"
                    f"⭐ پریمیوم: {'بله' if getattr(me, 'premium', False) else 'خیر'}\n")
        except Exception:
            live = "📛 (اطلاعات زنده الان در دسترس نیست)\n"
    running = "🟢 در حال اجرا" if c else "🔴 متوقف (نشست بالا نیومده)"
    power = "روشن" if st.get("selfon", "1") == "1" else "خاموش (هزینه کسر میشه)"
    paid = await db.user_is_active(tuid)
    mutes, emojis = len(await db.uprefix(tuid, "mute:")), len(await db.uprefix(tuid, "emoji:"))
    notes = len(await db.uprefix(tuid, "note:"))
    tx = "\n".join(f"{'➕' if r['amount'] > 0 else '➖'} {abs(r['amount']):,} — "
                   f"{esc(next((v for k, v in REASONS.items() if r['reason'].startswith(k)), r['reason']))}"
                   for r in f["tx"]) or "—"
    created = u["created"].strftime("%Y-%m-%d %H:%M") if u["created"] else "—"
    txt = (head + live +
           f"📱 شماره: <code>{esc(u['phone'] or '—')}</code>\n"
           f"📅 تاریخ ساخت سلف: {created}\n"
           f"⚙️ وضعیت سلف: {running} | {power}\n"
           f"💳 دسترسی: {'✅ فعال' if paid else '⛔ الماس تموم شده'}\n"
           f"💎 الماس: {bal}\n⏳ زمان باقی‌مانده: {await left_text(tuid)}\n"
           f"👥 دعوت‌شده‌ها: {f['invited']}\n\n"
           f"<b>تنظیمات:</b>\n😴 منشی {flag('afk')} | 🗑 ضدحذف {flag('antidel', True)} | ⏳ تایمردار {flag('ttl', True)}\n"
           f"💬 پاسخ کلمه‌ای {flag('kw', True)} ({f['replies']}) | 👁 سین {flag('autoseen')} | ❤️ ریاکشن {flag('autoreact')}\n"
           f"🔒 قفل پیوی {flag('lockpv')} | 🕒 ساعت: اسم {flag('clock')} فامیل {flag('clock_last')} بیو {flag('clock_bio')}\n"
           f"🔇 سکوت: {mutes} | ⭐ ایموجی ثبت‌شده: {emojis} | 🗒 یادداشت: {notes}\n\n"
           f"<b>آخرین تراکنش‌ها:</b>\n{tx}")
    return txt[:3900], rows


# =========================================================== background / boot
# ---- hourly billing: while the self is logged in (running), even if switched off; owner is free
tick_ts: dict[int, float] = {}
_saved: dict[int, float] = {}


def billing_reset(uid):
    tick_ts[uid] = time.time()


async def notify(uid, text):
    try:
        await bot.send_message(uid, text)
        await send_menu(uid)
    except Exception:
        pass


async def bill_tick(uid, now):
    last = tick_ts.get(uid)
    tick_ts[uid] = now
    if last is None:
        return                                   # first time we see this self: count from now
    dt = min(now - last, 120)                    # never bill downtime / long stalls
    remaining = await db.remaining_seconds(uid)
    if remaining <= 0:
        if await db.uget(uid, "exp_notified", "0") != "1":
            await db.uset(uid, "exp_notified", "1")
            await notify(uid, "⛔ الماس‌هات تموم شد و سلف متوقف شد.\n"
                              "با «خرید الماس» یا «کد هدیه» شارژ کن؛ سلف خودکار دوباره شروع می‌کنه.")
        return
    if await db.uget(uid, "exp_notified", "0") == "1":
        await db.uset(uid, "exp_notified", "0")
    a = await db.get_acc(uid) + dt
    db.acc[uid] = a
    if a >= 3600:
        if await db.bill_hours(uid, 1):
            db.acc[uid] = a - 3600
        else:                                    # less than a full hour of diamonds left: use what is left
            db.acc[uid] = min(a, await db.time_units(uid) * 3600 // db.RATE)
        await db.save_acc(uid)
        _saved[uid] = now
    elif now - _saved.get(uid, 0) > 300:
        await db.save_acc(uid)
        _saved[uid] = now
    left = remaining - dt
    if left < 3 * 3600:
        if await db.uget(uid, "low_notified", "0") != "1":
            await db.uset(uid, "low_notified", "1")
            await notify(uid, f"⚠️ فقط {db.fmt_secs(max(left, 0))} از سلفت مونده. برای ادامه الماس بخر یا کد هدیه بزن.")
    elif await db.uget(uid, "low_notified", "0") == "1":
        await db.uset(uid, "low_notified", "0")


async def billing_loop():
    while True:
        await asyncio.sleep(30)
        now = time.time()
        for uid in list(selfbot.clients):
            if uid in ADMINS:
                continue
            try:
                await bill_tick(uid, now)
            except Exception:
                log.exception("billing tick %s", uid)


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
    asyncio.create_task(billing_loop())
    log.info("running: %s selfs", len(selfbot.clients))
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
