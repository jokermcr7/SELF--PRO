"""Game extras for the special game group:

  🙈 چالش            two players lock a SECRET number 1..9 (only they see it); random.org draws the target number;
                     the closer one on the circle wins. On a circle with an odd number of points both players
                     have EXACTLY the same chance whatever they pick, so it is pure luck.
"""
import asyncio
import html
import itertools
import logging
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import botapi
import db
import utils
from botapi import GREEN, RED, BLUE, btn, kb

log = logging.getLogger("gameplus")
TZ = ZoneInfo("Asia/Tehran")
LN = "┄┄┄┄┄┄┄┄┄┄┄┄┄┄"
BRAND = os.getenv("GAME_BRAND", "𝚾𝚻𝚳 𝙎𝙀𝙇𝙁")
CH_N = 9                                                              # secret numbers 1..9 (odd => perfectly fair)
CH_JOIN_TIMEOUT, CH_PICK_TIMEOUT = 180, 90

_ids = itertools.count(int(time.time()) % 10 ** 8)
challenges: dict[int, dict] = {}


def esc(s):
    return html.escape(utils.clean_name(str(s)))


def ulink(uid, name):
    """Clickable name: tapping it opens that user's profile."""
    return f'<a href="tg://user?id={int(uid)}">{esc(name)}</a>' if uid else esc(name)


def today() -> str:
    return datetime.now(TZ).date().isoformat()


def week_key(d=None) -> str:
    """Tournament weeks start on Saturday (Iran)."""
    d = d or datetime.now(TZ).date()
    return (d - timedelta(days=(d.weekday() + 2) % 7)).isoformat()


# ------------------------------------------------------------------ 🙈 hidden-number challenge
def _dist(a, t):
    d = (a - t) % CH_N
    return min(d, CH_N - d)


def register(bot, admin_ids, H):
    """H: helpers from groupgames (FEE, MIN_BET, MAX_BET, rnd_ints, rnd_bool, name_of, game_chat)."""
    rnd_ints, rnd_bool = H["rnd_ints"], H["rnd_bool"]
    FEE, MIN_BET, MAX_BET = H["FEE"], H["MIN_BET"], H["MAX_BET"]

    def prize(bet):
        pot = bet * 2
        return pot - pot * FEE // 100

    def head(C):
        return (f"<b><i>• — {html.escape(BRAND)} — •</i></b>\n\n<b>🙈 چالش عدد مخفی</b>\n{LN}\n"
                f"💎 ورودی هر نفر: <b>{C['bet']:,}</b> | 🏆 جایزه‌ی برنده: <b>{prize(C['bet']):,}</b> 💎 (مالیات {FEE}٪)\n"
                f"👤 سازنده: {ulink(*C['p'][0])}\n")

    def num_markup(cid):
        rows = [[btn(str(i), f"cx:{cid}:p:{i}", BLUE) for i in range(r, r + 3)] for r in (1, 4, 7)]
        return kb(rows)

    async def refund(C, why):
        for u, _ in C["p"]:
            await db.add(u, C["bet"], "challenge_refund")
        try:
            await botapi.edit_banner(C["chat"], C["msg"], head(C) + f"\n{why}\n💎 ورودی‌ها برگشت داده شد.", kb([]))
        except Exception:                                              # noqa: BLE001
            pass

    async def expire(cid, secs):
        await asyncio.sleep(secs)
        C = challenges.get(cid)
        if not C:
            return
        async with C["lock"]:
            if C["state"] == "open":
                C["state"] = "done"
                challenges.pop(cid, None)
                await refund(C, "⌛ حریفی نیومد؛ بازی لغو شد.")
            elif C["state"] == "pick":
                for u, _ in C["p"]:                                    # missing picks are drawn at random
                    if u not in C["picks"]:
                        C["picks"][u] = (await rnd_ints(1, 1, CH_N))[0]
                C["auto"] = True
                C["state"] = "run"
                await run(cid)

    async def run(cid):
        C = challenges.get(cid)
        try:
            (u1, n1), (u2, n2) = C["p"]
            a, b = C["picks"][u1], C["picks"][u2]
            await botapi.edit_banner(C["chat"], C["msg"], head(C) + "\n🎲 قرعه‌ی عدد هدف از random.org ...", kb([]))
            await asyncio.sleep(2)
            t = None
            for _ in range(8):                                         # redraw if both are equally close
                t = (await rnd_ints(1, 1, CH_N))[0]
                if _dist(a, t) != _dist(b, t):
                    break
            if a == b or _dist(a, t) == _dist(b, t):
                win = 0 if await rnd_bool() else 1
                how = "مساوی شد؛ برنده با قرعه‌ی سکه مشخص شد 🪙"
            else:
                win = 0 if _dist(a, t) < _dist(b, t) else 1
                how = "نزدیک‌تر به عدد هدف برنده‌ست"
            wid, wname = C["p"][win]
            pz = prize(C["bet"])
            await db.add(wid, pz, "challenge_win")
            text = (head(C) + f"\n🔓 عددهای مخفی:\n{'🏆' if win == 0 else '▫️'} {ulink(u1, n1)}: <b>{a}</b>\n"
                    f"{'🏆' if win == 1 else '▫️'} {ulink(u2, n2)}: <b>{b}</b>\n🎯 عدد هدف: <b>{t}</b>\n"
                    f"<i>({how}{' | انتخاب‌های ناقص خودکار پر شد' if C.get('auto') else ''})</i>\n\n"
                    f"🏆 برنده: <b>{ulink(wid, wname)}</b>\n❌ بازنده: <b>{ulink(*C['p'][1 - win])}</b>\n💎 جایزه: <b>{pz:,}</b>")
            await botapi.edit_banner(C["chat"], C["msg"], text,
                                     kb([[btn(f"🔁 چالش جدید ({C['bet']:,} 💎)", f"cx:0:n:{C['bet']}", GREEN)]]))
        except Exception:                                              # noqa: BLE001
            log.exception("challenge failed, refunding")
            await refund(C, "⚠️ خطا توی بازی.")
        finally:
            challenges.pop(cid, None)

    async def create(chat, reply_to, uid, name, bet):
        if not MIN_BET <= bet <= MAX_BET:
            return f"مبلغ ورودی حداقل {MIN_BET:,} 💎 باشه"
        if sum(1 for C in challenges.values() if C["p"][0][0] == uid) >= 1:
            return "تو یه چالش باز داری؛ اول اون تموم بشه"
        if await db.try_spend(uid, bet, "challenge_bet") is None:
            return "💎 موجودی کافی نیست"
        cid = next(_ids)
        C = {"id": cid, "chat": chat, "bet": bet, "p": [(uid, name)], "picks": {}, "state": "open", "lock": asyncio.Lock(), "msg": 0}
        text = (head(C) + "\nدو نفر هر کدوم یه <b>عدد مخفی ۱ تا ۹</b> انتخاب می‌کنن (فقط خودشون می‌بینن 🙈)؛ "
                          "بعد random.org یه عدد هدف می‌ده و نزدیک‌تر برنده‌ست. هر عددی که بزنی شانس دقیقاً ۵۰٪ ـه 🍀\n\n"
                          "منتظر حریف •••")
        m = await botapi.send_banner(chat, text, kb([[btn("پیوستن ✅", f"cx:{cid}:j", GREEN), btn("لغو ❌", f"cx:{cid}:c", RED)]]),
                                     reply_to=reply_to)
        C["msg"] = m["message_id"]
        challenges[cid] = C
        C["task"] = asyncio.create_task(expire(cid, CH_JOIN_TIMEOUT))
        return None

    async def handle(e, cmd, arg, uid, name):
        """Group command: چالش [مبلغ]. Returns True when handled."""
        if cmd == "chal":
            a = utils.to_en_digits(arg).split()
            bet = int(a[0]) if a and a[0].isdigit() else MIN_BET
            err = await create(e.chat_id, e.id, uid, name, bet)
            if err:
                await e.reply(err)
        return True

    @bot.on(__import__("telethon").events.CallbackQuery(pattern=rb"^cx:"))
    async def cb_chal(e):
        p = e.data.decode().split(":")
        cid, act, arg = int(p[1]), p[2], (p[3] if len(p) > 3 else "")
        uid = e.sender_id
        if act == "n":                                                 # «چالش جدید» button
            name = H["name_of"](await e.get_sender())
            await db.ensure_wallet(uid, name)
            err = await create(e.chat_id, None, uid, name, int(arg))
            return await e.answer(err or "🙈 چالش جدید ساخته شد", alert=bool(err))
        C = challenges.get(cid)
        if not C:
            return await e.answer("این چالش تموم شده", alert=True)
        async with C["lock"]:
            ids = [u for u, _ in C["p"]]
            if act == "c":
                if uid != ids[0] and uid not in admin_ids:
                    return await e.answer("فقط سازنده می‌تونه لغو کنه", alert=True)
                if C["state"] != "open":
                    return await e.answer("بازی شروع شده", alert=True)
                C["state"] = "done"
                C["task"].cancel()
                challenges.pop(cid, None)
                await e.answer("لغو شد")
                return await refund(C, "❌ سازنده بازی رو لغو کرد.")
            if act == "j":
                if C["state"] != "open":
                    return await e.answer("بازی شروع شده", alert=True)
                if uid in ids:
                    return await e.answer("تو سازنده‌ای 😉", alert=True)
                if await db.try_spend(uid, C["bet"], "challenge_bet") is None:
                    return await e.answer("💎 موجودی کافی نیست", alert=True)
                name = H["name_of"](await e.get_sender())
                await db.ensure_wallet(uid, name)
                C["p"].append((uid, name))
                C["state"] = "pick"
                C["task"].cancel()
                C["task"] = asyncio.create_task(expire(cid, CH_PICK_TIMEOUT))
                await e.answer("✅ پیوستی")
                return await botapi.edit_banner(
                    C["chat"], C["msg"], head(C) + f"👥 حریف: {ulink(uid, name)}\n\n🙈 حالا هر دو نفر یه عدد مخفی بزنید "
                    f"(تا {CH_PICK_TIMEOUT} ثانیه)؛ انتخابت فقط برای خودت نمایش داده میشه.", num_markup(cid))
            if act == "p":
                if C["state"] != "pick" or uid not in ids:
                    return await e.answer("الان نمیشه", alert=True)
                if uid in C["picks"]:
                    return await e.answer(f"قبلاً عدد مخفیت رو زدی: {C['picks'][uid]} 🔒", alert=True)
                C["picks"][uid] = int(arg)
                await e.answer(f"🔒 عدد مخفی تو: {arg}\nمنتظر حریف باش ...", alert=True)
                if len(C["picks"]) == 2:
                    C["state"] = "run"
                    C["task"].cancel()
                    return await run(cid)
                return await botapi.edit_banner(C["chat"], C["msg"], head(C) + f"👥 حریف: {ulink(*C['p'][1])}\n\n"
                                                f"🔒 یک نفر عددش رو قفل کرد؛ منتظر نفر دوم ...", num_markup(cid))

    return handle
