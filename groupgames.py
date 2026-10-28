"""Diamond games, played ONLY in the special game group. Runs on the manager bot."""
import asyncio
import html
import logging
import os
import random
import re
import time

from telethon import events, types

import botapi
import db
import utils
from botapi import GREEN, RED, BLUE, btn, kb

log = logging.getLogger("games")

MIN_GAME = 20
MAX_GAME = int(os.getenv("MAX_GAME_BET", "10000"))
FEE = int(os.getenv("GAME_FEE_PERCENT", "5"))          # tax: 88 pot -> 4 tax -> 84 prize
BRAND = os.getenv("BRAND", "Vip MMD")
_rng = random.SystemRandom()
LOBBY_TIMEOUT = 180
SIDE_MIN, SIDE_MAX = 5, 500

lobbies: dict[int, dict] = {}
_cool: dict[int, float] = {}
PERSIAN_CMDS = {"موجودی": "bal", "بازی": "game", "روزانه": "daily", "برترین": "top", "انتقال": "give"}
EN_CMDS = {"bal": "bal", "balance": "bal", "game": "game", "daily": "daily", "top": "top",
           "give": "give", "dice": "dice", "coin": "coin", "slot": "slot", "guess": "guess"}


def mention(uid, name):
    return f"[{utils.clean_name(name)}](tg://user?id={uid})"


def name_of(u):
    return utils.clean_name(getattr(u, "first_name", None) or getattr(u, "username", None) or str(getattr(u, "id", "")))


async def recover(bot):
    """After a crash/restart: refund every bet that was locked in an open lobby."""
    for r in await db.lobby_all():
        for uid in r["players"]:
            await db.add(uid, r["bet"], "game_refund_restart")
        await db.lobby_delete(r["id"])


def register(bot, admin_ids):
    async def game_chat():
        return int(await db.get("game_group", os.getenv("GAME_GROUP_ID", "0")) or 0)

    # ------------------------------------------------------------ lobby helpers
    ORD = {2: "دوم", 3: "سوم"}

    def pot_of(L):
        return L["bet"] * L["kind"]

    def tax_of(L):
        return pot_of(L) * FEE // 100

    def prize_of(L):
        return pot_of(L) - tax_of(L)

    def esc(n):
        return html.escape(utils.clean_name(n))

    def lobby_text(L):
        """Same layout as the reference video: label line, value on the next line."""
        creator = L["players"][0][1]
        joined = len(L["players"])
        t = (f"<b><i>• — {html.escape(BRAND)} — •</i></b>\n\n"
             f"<b>• بازی جدید 🎲</b>\n"
             f"👤 سازنده:\n{esc(creator)}\n\n"
             f"👥 تعداد بازیکنان:\n{L['kind']} 🇮🇷\n\n"
             f"💎 مقدار:\n{L['bet']} 💎\n\n"
             f"🏆 جایزه برنده:\n{prize_of(L)} 💎\n\n"
             f"➖ مالیات:\n{tax_of(L)} 💎\n")
        if joined > 1:
            t += "\n🙋 پیوسته‌ها: " + "، ".join(esc(n) for _, n in L["players"][1:]) + "\n"
        t += f"\nدر انتظار پیوستن بازیکن {ORD[joined + 1]} •••"
        return t

    def lobby_markup(lid, L):
        rows = [[btn("پیوستن ✅", f"lj:{lid}", GREEN), btn("لغو بازی ❌", f"lc:{lid}", RED)]]
        if L["kind"] == 2:
            rows.append([btn("بازی با بات 🎮", f"lb:{lid}", BLUE)])
        return kb(rows)

    def result_text(winner, losers):
        return (f"<b>شرط تمام شد</b>\n\nبرنده: {esc(winner)}\n"
                f"بازنده: {'، '.join(esc(n) for n in losers)}")

    def result_markup(prize, shown_bal):
        return kb([
            [btn(f"الماس {prize} 💎", f"gi:p:{prize}", BLUE), btn("جایزه برنده 🏆", f"gi:p:{prize}", GREEN)],
            [btn(f"الماس {db.fmt(shown_bal)} 💎", "gi:b", BLUE), btn("موجودی شما ♦️", "gi:b", RED)],
        ])

    async def edit_lobby(L, text, markup=None):
        try:
            await botapi.edit_banner(L["chat"], L["msg"], text, markup)
        except Exception:
            log.exception("edit lobby failed")

    async def cancel_lobby(lid, reason):
        L = lobbies.get(lid)
        if not L:
            return
        async with L["lock"]:
            if L["state"] != "open":
                return
            L["state"] = "closed"
            for u, _ in L["players"]:
                if u:
                    await db.add(u, L["bet"], "game_refund")
            lobbies.pop(lid, None)
            await db.lobby_delete(lid)
        await edit_lobby(L, reason)

    async def lobby_timeout(lid):
        await asyncio.sleep(LOBBY_TIMEOUT)
        await cancel_lobby(lid, "⌛ زمان تموم شد؛ شرط‌ها برگشت داده شد.")

    async def run_game(lid):
        """Instant, fair draw (every seat has equal odds); the lobby message is edited into the result."""
        L = lobbies[lid]
        try:
            winner = _rng.choice(L["players"])
            losers = [p for p in L["players"] if p is not winner]
            prize = prize_of(L)
            wid, wname = winner
            if wid:
                await db.add(wid, prize, "game_win")
            creator_id = L["players"][0][0]
            shown = await db.balance(creator_id)
            await edit_lobby(L, result_text(wname, [n for _, n in losers]), result_markup(prize, shown))
        except Exception:
            log.exception("game failed, refunding")
            for u, _ in L["players"]:
                if u:
                    await db.add(u, L["bet"], "game_refund_error")
            await edit_lobby(L, "⚠️ خطا توی بازی؛ شرط‌ها برگشت داده شد.")
        finally:
            lobbies.pop(lid, None)
            await db.lobby_delete(lid)

    async def open_lobby(e, uid, name, bet, kind):
        """Creator pays the bet, then the lobby message (replying to the command) is sent."""
        if await db.try_spend(uid, bet, "game_bet") is None:
            return await e.reply("💎 موجودی کافی نیست")
        lid = await db.lobby_create(e.chat_id, bet, kind, uid)
        L = {"chat": e.chat_id, "msg": 0, "bet": bet, "kind": kind, "creator": uid,
             "players": [(uid, name)], "lock": asyncio.Lock(), "state": "open"}
        try:
            m = await botapi.send_banner(e.chat_id, lobby_text(L), lobby_markup(lid, L), reply_to=e.id)
        except Exception:
            log.exception("lobby send failed")
            await db.add(uid, bet, "game_refund_error")
            await db.lobby_delete(lid)
            return await e.reply("⚠️ ساخت بازی ناموفق بود؛ شرطت برگشت داده شد.")
        L["msg"] = m["message_id"]
        lobbies[lid] = L
        L["task"] = asyncio.create_task(lobby_timeout(lid))

    # ------------------------------------------------------------ simple games vs bot
    def bet_of(arg):
        a = utils.to_en_digits(arg or "").split()
        if not a or not a[0].isdigit():
            return None
        b = int(a[0])
        return b if SIDE_MIN <= b <= SIDE_MAX else None

    async def g_dice(e, uid, name, arg):
        bet = bet_of(arg)
        if not bet:
            return await e.reply(f"مثال: `/dice 20` ({SIDE_MIN} تا {SIDE_MAX} 💎)")
        if await db.try_spend(uid, bet, "dice_bet") is None:
            return await e.reply("💎 الماست کافی نیست")
        m1 = await bot.send_message(e.chat_id, file=types.InputMediaDice("🎲"))
        await asyncio.sleep(3.3)
        m2 = await bot.send_message(e.chat_id, file=types.InputMediaDice("🎲"))
        await asyncio.sleep(3.3)
        you, house = m1.media.value, m2.media.value
        if you > house:
            bal = await db.add(uid, bet * 2, "dice_win")
            txt = f"🏆 بردی! +{bet} 💎"
        elif you == house:
            bal = await db.add(uid, bet, "dice_tie")
            txt = "🤝 مساوی، شرطت برگشت"
        else:
            bal = await db.balance(uid)
            txt = f"💥 باختی! -{bet} 💎"
        await e.reply(f"تو: {you} | ربات: {house}\n{txt}\nموجودی: {db.fmt(bal)} 💎")

    async def g_coin(e, uid, name, arg):
        bet = bet_of(arg)
        if not bet:
            return await e.reply(f"مثال: `/coin 20` ({SIDE_MIN} تا {SIDE_MAX} 💎)")
        if await db.try_spend(uid, bet, "coin_bet") is None:
            return await e.reply("💎 الماست کافی نیست")
        if random.random() < 0.48:
            bal = await db.add(uid, bet * 2, "coin_win")
            await e.reply(f"🪙 شیر! +{bet} 💎 | موجودی: {db.fmt(bal)}")
        else:
            await e.reply(f"🪙 خط! -{bet} 💎 | موجودی: {db.fmt(await db.balance(uid))}")

    async def g_slot(e, uid, name, arg):
        bet = bet_of(arg)
        if not bet:
            return await e.reply(f"مثال: `/slot 20` ({SIDE_MIN} تا {SIDE_MAX} 💎)")
        if await db.try_spend(uid, bet, "slot_bet") is None:
            return await e.reply("💎 الماست کافی نیست")
        m = await bot.send_message(e.chat_id, file=types.InputMediaDice("🎰"))
        await asyncio.sleep(4)
        v = m.media.value
        if v == 64:
            win, label = bet * 10, "🎉 جکپات 777"
        elif v in (1, 22, 43):
            win, label = bet * 4, "✨ سه تا مثل هم"
        else:
            win, label = 0, "😢 نشد"
        if win:
            bal = await db.add(uid, win, "slot_win")
            await e.reply(f"{label}\n+{win - bet} 💎 | موجودی: {db.fmt(bal)}")
        else:
            await e.reply(f"{label}\n-{bet} 💎 | موجودی: {db.fmt(await db.balance(uid))}")

    async def g_guess(e, uid, name, arg):
        a = utils.to_en_digits(arg or "").split()
        if len(a) != 2 or not a[1].isdigit() or not 1 <= int(a[1]) <= 5 or not bet_of(a[0]):
            return await e.reply("مثال: `/guess 20 3` (عدد ۱ تا ۵)")
        bet = int(a[0])
        if await db.try_spend(uid, bet, "guess_bet") is None:
            return await e.reply("💎 الماست کافی نیست")
        n = random.randint(1, 5)
        if n == int(a[1]):
            bal = await db.add(uid, bet * 4, "guess_win")
            await e.reply(f"🎯 عدد {n} بود! +{bet * 3} 💎 | موجودی: {db.fmt(bal)}")
        else:
            await e.reply(f"❌ عدد {n} بود. -{bet} 💎 | موجودی: {db.fmt(await db.balance(uid))}")

    # ------------------------------------------------------------ transfer: reply + "انتقال 25"
    async def do_give(e, uid, name, arg):
        a = arg.split()
        r = await e.get_reply_message() if e.is_reply else None
        if not r or not r.sender_id:
            return await e.reply("روی پیام کاربر ریپلای کن و بنویس: `انتقال 25`")
        if not a or not a[0].isdigit() or int(a[0]) <= 0:
            return await e.reply("مقدار نامعتبره. مثال: `انتقال 25`")
        amt = int(a[0])
        if amt > 10 ** 9:
            return await e.reply("مقدار خیلی بزرگه")
        if r.sender_id == uid:
            return await e.reply("به خودت نمی‌تونی انتقال بدی 😉")
        target = await r.get_sender()
        if getattr(target, "bot", False):
            return await e.reply("به ربات نمی‌تونی انتقال بدی 🤖")
        tname = name_of(target)
        res = await db.transfer(uid, r.sender_id, amt, tname)
        if res is None:
            return await e.reply(f"💎 الماست کافی نیست (موجودی: {db.fmt(await db.balance(uid))})")
        sb, tb = res
        await e.reply(
            f"✅ **انتقال انجام شد**\n\n"
            f"👤 از: {mention(uid, name)}\n"
            f"🎁 به: {mention(r.sender_id, tname)}\n"
            f"💎 مقدار: **{amt:,}**\n\n"
            f"موجودی تو: {db.fmt(sb)}")

    # ------------------------------------------------------------ group messages
    @bot.on(events.NewMessage(func=lambda e: e.is_group))
    async def on_msg(e):
        t = utils.to_en_digits((e.raw_text or "").strip())
        if not t or not e.sender_id:
            return
        parts = t.split(None, 1)
        first = parts[0]
        arg = parts[1] if len(parts) > 1 else ""

        if re.match(r"^/setgame(@\w+)?$", first) and e.sender_id in admin_ids:
            await db.set_("game_group", e.chat_id)
            return await e.reply("🎮 این گروه شد «گپ مخصوص بازی».")

        if first.startswith("/"):
            cmd = EN_CMDS.get(first[1:].split("@")[0].lower())
        else:
            cmd = PERSIAN_CMDS.get(first)
        if not cmd:
            return
        if cmd not in ("bal", "give") and e.chat_id != await game_chat():
            return  # games exist only in the special group (balance + transfer work in any group)
        if time.time() - _cool.get(e.sender_id, 0) < 1.5:
            return
        _cool[e.sender_id] = time.time()

        uid = e.sender_id
        s = await e.get_sender()
        name = name_of(s)
        await db.ensure_wallet(uid, name)

        if cmd == "bal":
            r = await e.get_reply_message() if e.is_reply else None
            if r and r.sender_id:
                tid, tname = r.sender_id, name_of(await r.get_sender())
            else:
                tid, tname = uid, name
            return await e.reply(f"💎 موجودی {mention(tid, tname)}: **{db.fmt(await db.balance(tid))}**")

        if cmd == "game":
            a = arg.split()
            if not a or not a[0].isdigit():
                return await e.reply(f"مثال: `بازی 44` یا `بازی 44 3` (سه‌نفره) — حداقل {MIN_GAME} 💎")
            bet = int(a[0])
            kind = int(a[1]) if len(a) > 1 and a[1] in ("2", "3") else 2
            if bet < MIN_GAME or bet > MAX_GAME:
                return await e.reply(f"شرط باید بین {MIN_GAME} تا {MAX_GAME} 💎 باشه")
            return await open_lobby(e, uid, name, bet, kind)

        if cmd == "daily":
            bal, wait = await db.claim_daily(uid, name)
            if bal is None:
                return await e.reply(f"⏳ {wait // 3600} ساعت و {wait % 3600 // 60} دقیقه دیگه")
            return await e.reply(f"🎁 {db.DAILY_BONUS} 💎 گرفتی! موجودی: {db.fmt(bal)}")

        if cmd == "top":
            rows = await db.top(10)
            lines = [f"{i}. {utils.clean_name(r['name'] or str(r['user_id']))} — {r['diamonds']:,} 💎" for i, r in enumerate(rows, 1)]
            return await e.reply("🏆 **برترین‌ها**\n" + ("\n".join(lines) or "هنوز کسی نیست"))

        if cmd == "give":
            return await do_give(e, uid, name, arg)

        games = {"dice": g_dice, "coin": g_coin, "slot": g_slot, "guess": g_guess}
        if cmd in games:
            return await games[cmd](e, uid, name, arg)

    # ------------------------------------------------------------ callbacks
    @bot.on(events.CallbackQuery(pattern=rb"^lj:"))
    async def cb_join(e):
        lid = int(e.data.decode().split(":")[1])
        L = lobbies.get(lid)
        if not L:
            return await e.answer("این بازی تموم شده", alert=True)
        uid = e.sender_id
        start = False
        async with L["lock"]:
            if L["state"] != "open":
                return await e.answer("بازی شروع شده", alert=True)
            if any(u == uid for u, _ in L["players"]):
                return await e.answer("تو قبلاً پیوستی 😉", alert=True)
            if await db.try_spend(uid, L["bet"], "game_bet") is None:
                return await e.answer("💎 موجودی کافی نیست", alert=True)
            name = name_of(await e.get_sender())
            await db.ensure_wallet(uid, name)
            L["players"].append((uid, name))
            await db.lobby_add(lid, uid)
            if len(L["players"]) >= L["kind"]:
                L["state"] = "running"
                start = True
        if start:
            L["task"].cancel()
            await e.answer("🎲")
            return await run_game(lid)
        await e.answer("پیوستی ✅")
        await edit_lobby(L, lobby_text(L), lobby_markup(lid, L))

    @bot.on(events.CallbackQuery(pattern=rb"^lb:"))
    async def cb_bot(e):
        lid = int(e.data.decode().split(":")[1])
        L = lobbies.get(lid)
        if not L:
            return await e.answer("این بازی تموم شده", alert=True)
        if e.sender_id != L["creator"]:
            return await e.answer("فقط سازنده می‌تونه با ربات بازی کنه", alert=True)
        async with L["lock"]:
            if L["state"] != "open" or L["kind"] != 2 or len(L["players"]) != 1:
                return await e.answer("الان نمیشه", alert=True)
            L["state"] = "running"
            L["players"].append((0, "ربات"))
        L["task"].cancel()
        await e.answer("🤖")
        await run_game(lid)

    @bot.on(events.CallbackQuery(pattern=rb"^lc:"))
    async def cb_cancel(e):
        lid = int(e.data.decode().split(":")[1])
        L = lobbies.get(lid)
        if not L:
            return await e.answer("این بازی تموم شده", alert=True)
        if e.sender_id != L["creator"] and e.sender_id not in admin_ids:
            return await e.answer("فقط سازنده می‌تونه لغو کنه", alert=True)
        L["task"].cancel()
        await cancel_lobby(lid, "❌ بازی لغو شد؛ شرط‌ها برگشت داده شد.")
        await e.answer("لغو شد")

    @bot.on(events.CallbackQuery(pattern=rb"^gi:"))
    async def cb_info(e):
        """Info buttons on the result message."""
        p = e.data.decode().split(":")
        if p[1] == "p":
            return await e.answer(f"🏆 جایزه برنده: {p[2]} 💎", alert=True)
        await e.answer(f"💎 موجودی شما: {db.fmt(await db.balance(e.sender_id))}", alert=True)
