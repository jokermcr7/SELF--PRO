"""Diamond games, played ONLY in the special game group. Runs on the manager bot."""
import asyncio
import logging
import os
import random
import re
import time

from telethon import Button, events, types

import assets
import db
import utils

log = logging.getLogger("games")

MIN_GAME = 20
MAX_GAME = int(os.getenv("MAX_GAME_BET", "10000"))
FEE = int(os.getenv("GAME_FEE_PERCENT", "0"))
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
    def prize_of(L):
        pot = L["bet"] * L["kind"]
        return pot - pot * FEE // 100

    def lobby_text(L):
        plist = "\n".join(f"{i}. {mention(u, n)}" for i, (u, n) in enumerate(L["players"], 1))
        need = L["kind"] - len(L["players"])
        extra = "\n🤖 سازنده می‌تونه با ربات هم بازی کنه." if L["kind"] == 2 else ""
        return (f"🎲 **بازی {L['kind']} نفره** — شرط هر نفر: **{L['bet']}** 💎\n"
                f"🏆 جایزه برنده: **{prize_of(L)}** 💎 (شانس همه برابر)\n\n{plist}\n\n"
                f"⏳ منتظر {need} نفر دیگه...{extra}")

    def lobby_buttons(lid, L):
        rows = [[Button.inline(f"➕ پیوستن ({len(L['players'])}/{L['kind']})", f"lj:{lid}".encode())]]
        if L["kind"] == 2:
            rows.append([Button.inline("🤖 بازی با ربات", f"lb:{lid}".encode())])
        rows.append([Button.inline("❌ لغو (سازنده)", f"lc:{lid}".encode())])
        return rows

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
        try:
            await bot.edit_message(L["chat"], L["msg"], reason, buttons=None)
        except Exception:
            pass

    async def lobby_timeout(lid):
        await asyncio.sleep(LOBBY_TIMEOUT)
        await cancel_lobby(lid, "⌛ زمان تموم شد؛ شرط‌ها برگشت داده شد.")

    async def run_game(lid):
        L = lobbies[lid]
        chat = L["chat"]
        try:
            contenders = list(L["players"])
            lines = []
            rnd = 1
            while len(contenders) > 1:
                rolls = {}
                for u, n in contenders:
                    await bot.send_message(chat, f"🎲 نوبت {mention(u, n) if u else n}")
                    m = await bot.send_message(chat, file=types.InputMediaDice("🎲"))
                    rolls[u] = m.media.value
                    await asyncio.sleep(3.3)
                best = max(rolls.values())
                lines.append(f"دور {rnd}: " + " | ".join(f"{utils.clean_name(n)} {rolls[u]}" for u, n in contenders))
                contenders = [p for p in contenders if rolls[p[0]] == best]
                rnd += 1
            wid, wname = contenders[0]
            prize = prize_of(L)
            if wid:
                bal = await db.add(wid, prize, "game_win")
                end = f"🏆 برنده: {mention(wid, wname)}\n💰 جایزه: **{prize}** 💎 (موجودی: {bal})"
            else:
                end = "🤖 ربات برد! شرطت سوخت 😅"
            cap = (end + "\n\n📋 " + "\n".join(lines))[:1000]
            m = await bot.send_message(chat, cap, file=await assets.banner())
            assets.remember(m)
        except Exception:
            log.exception("game failed, refunding")
            for u, _ in L["players"]:
                if u:
                    await db.add(u, L["bet"], "game_refund_error")
            await bot.send_message(chat, "⚠️ خطا توی بازی؛ شرط‌ها برگشت داده شد.")
        finally:
            lobbies.pop(lid, None)
            await db.lobby_delete(lid)

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
        await e.reply(f"تو: {you} | ربات: {house}\n{txt}\nموجودی: {bal} 💎")

    async def g_coin(e, uid, name, arg):
        bet = bet_of(arg)
        if not bet:
            return await e.reply(f"مثال: `/coin 20` ({SIDE_MIN} تا {SIDE_MAX} 💎)")
        if await db.try_spend(uid, bet, "coin_bet") is None:
            return await e.reply("💎 الماست کافی نیست")
        if random.random() < 0.48:
            bal = await db.add(uid, bet * 2, "coin_win")
            await e.reply(f"🪙 شیر! +{bet} 💎 | موجودی: {bal}")
        else:
            await e.reply(f"🪙 خط! -{bet} 💎 | موجودی: {await db.balance(uid)}")

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
            await e.reply(f"{label}\n+{win - bet} 💎 | موجودی: {bal}")
        else:
            await e.reply(f"{label}\n-{bet} 💎 | موجودی: {await db.balance(uid)}")

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
            await e.reply(f"🎯 عدد {n} بود! +{bet * 3} 💎 | موجودی: {bal}")
        else:
            await e.reply(f"❌ عدد {n} بود. -{bet} 💎 | موجودی: {await db.balance(uid)}")

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
        if e.chat_id != await game_chat():
            return  # games exist only in the special group
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
            return await e.reply(f"💎 موجودی {mention(tid, tname)}: **{await db.balance(tid)}**")

        if cmd == "game":
            a = arg.split()
            if not a or not a[0].isdigit():
                return await e.reply(f"مثال: `بازی 20` (حداقل {MIN_GAME} 💎)")
            bet = int(a[0])
            if bet < MIN_GAME or bet > MAX_GAME:
                return await e.reply(f"شرط باید بین {MIN_GAME} تا {MAX_GAME} 💎 باشه")
            if await db.balance(uid) < bet:
                return await e.reply("💎 موجودی کافی نیست")
            m = await e.reply(
                f"🎮 **بازی {bet} 💎** — {mention(uid, name)} نوع بازی رو انتخاب کن:",
                file=await assets.banner(),
                buttons=[[Button.inline("👥 دونفره", f"gm:{uid}:{bet}:2".encode()),
                          Button.inline("👥👤 سه‌نفره", f"gm:{uid}:{bet}:3".encode())]])
            assets.remember(m)
            return m

        if cmd == "daily":
            bal, wait = await db.claim_daily(uid, name)
            if bal is None:
                return await e.reply(f"⏳ {wait // 3600} ساعت و {wait % 3600 // 60} دقیقه دیگه")
            return await e.reply(f"🎁 {db.DAILY_BONUS} 💎 گرفتی! موجودی: {bal}")

        if cmd == "top":
            rows = await db.top(10)
            lines = [f"{i}. {r['name'] or r['user_id']} — {r['diamonds']} 💎" for i, r in enumerate(rows, 1)]
            return await e.reply("🏆 **برترین‌ها**\n" + ("\n".join(lines) or "هنوز کسی نیست"))

        if cmd == "give":
            r = await e.get_reply_message() if e.is_reply else None
            a = arg.split()
            if not r or not r.sender_id or not a or not a[0].isdigit() or int(a[0]) <= 0 or r.sender_id == uid:
                return await e.reply("روی پیام طرف ریپلای کن: `/give 10`")
            amt = int(a[0])
            if await db.try_spend(uid, amt, "transfer_out") is None:
                return await e.reply("💎 الماست کافی نیست")
            await db.add(r.sender_id, amt, "transfer_in", name=name_of(await r.get_sender()))
            return await e.reply(f"✅ {amt} 💎 منتقل شد")

        games = {"dice": g_dice, "coin": g_coin, "slot": g_slot, "guess": g_guess}
        if cmd in games:
            return await games[cmd](e, uid, name, arg)

    # ------------------------------------------------------------ callbacks
    @bot.on(events.CallbackQuery(pattern=rb"^gm:"))
    async def cb_gm(e):
        _, creator, bet, kind = e.data.decode().split(":")
        creator, bet, kind = int(creator), int(bet), int(kind)
        uid = e.sender_id
        if uid != creator:
            return await e.answer("این بازی مال تو نیست 😉", alert=True)
        if await db.try_spend(uid, bet, "game_bet") is None:
            return await e.answer("💎 موجودی کافی نیست", alert=True)
        name = name_of(await e.get_sender())
        lid = await db.lobby_create(e.chat_id, bet, kind, uid)
        L = {"chat": e.chat_id, "msg": e.message_id, "bet": bet, "kind": kind, "creator": uid,
             "players": [(uid, name)], "lock": asyncio.Lock(), "state": "open"}
        lobbies[lid] = L
        L["task"] = asyncio.create_task(lobby_timeout(lid))
        await e.edit(lobby_text(L), buttons=lobby_buttons(lid, L))

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
            L["players"].append((uid, name_of(await e.get_sender())))
            await db.lobby_add(lid, uid)
            if len(L["players"]) >= L["kind"]:
                L["state"] = "running"
                start = True
        L["task"].cancel() if start else None
        if start:
            await e.edit("🎲 بازیکن‌ها کامل شدن، بازی شروع شد!", buttons=None)
            await e.answer()
            return await run_game(lid)
        await e.edit(lobby_text(L), buttons=lobby_buttons(lid, L))
        await e.answer("پیوستی ✅")

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
            L["players"].append((0, "🤖 ربات"))
        L["task"].cancel()
        await e.edit("🤖 بازی با ربات شروع شد!", buttons=None)
        await e.answer()
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
