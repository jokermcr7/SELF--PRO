"""Diamond games, ONLY in the special game group. Layout modeled on the reference video.
Every game is fair: all players (including the bot) have equal winning chances.
The only house edge is the TAX shown on the card (taken from the prize)."""
import asyncio
import logging
import os
import re
import secrets
import time

from telethon import Button, events, types

import assets
import db
import utils
from utils import money

log = logging.getLogger("games")

BRAND = os.getenv("BRAND", "MMD")
TAX_PERCENT = int(os.getenv("TAX_PERCENT", "5"))
MIN_BET = 20
MAX_BET = int(os.getenv("MAX_GAME_BET", "10000"))
LOBBY_TIMEOUT = 300
rng = secrets.SystemRandom()

lobbies: dict[int, dict] = {}
open_by_user: dict[int, int] = {}
_cool: dict[int, float] = {}
_busy: set = set()
RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}  # 18 red / 18 black, no zero

FA = {"بازی": "game", "موجودی": "bal", "انتقال": "give", "روزانه": "daily", "برترین": "top",
      "راهنما": "help", "تاس": "dice", "دارت": "dart", "بولینگ": "bowl", "سکه": "coin", "رولت": "roul"}
EN = {"game": "game", "bal": "bal", "balance": "bal", "give": "give", "daily": "daily", "top": "top",
      "help": "help", "dice": "dice", "dart": "dart", "bowl": "bowl", "coin": "coin", "roulette": "roul",
      "roul": "roul"}
DUELS = {"dice": "🎲", "dart": "🎯", "bowl": "🎳"}

GAME_HELP = f"""🎮 **راهنمای بازی {BRAND}**

`بازی 50` دونفره (با کاربر یا ربات)
`بازی 3 50` سه‌نفره
`تاس 50` | `دارت 50` | `بولینگ 50` دوئل با ربات
`سکه 50` شیر یا خط | `رولت 50` قرمز یا مشکی
`موجودی` (یا ریپلای روی کاربر) | `انتقال 25` (ریپلای) | `روزانه` | `برترین`

⚖️ شانس همه برابره. مالیات {TAX_PERCENT}٪ از جایزه کسر میشه.
حداقل شرط {MIN_BET} 💎"""


def split_pot(bet: int, n: int):
    pot = bet * n
    tax = pot * TAX_PERCENT // 100
    return pot, tax, pot - tax


def name_of(u) -> str:
    return utils.clean_name(getattr(u, "first_name", None) or getattr(u, "username", None) or str(getattr(u, "id", "")))


def mention(uid, name) -> str:
    return f"[{utils.clean_name(name)}](tg://user?id={uid})"


def parse_bet(tok):
    t = utils.to_en_digits(tok or "").strip()
    return int(t) if t.isdigit() else None


def parse_game(arg: str):
    t = [x for x in utils.to_en_digits(arg or "").split() if x not in ("نفره", "نفر", "نفری")]
    kind = 2
    if len(t) >= 2 and t[0] in ("3", "سه"):
        kind, t = 3, t[1:]
    elif len(t) >= 2 and t[0] in ("2", "دو"):
        t = t[1:]
    return kind, parse_bet(t[0] if t else None)


# ---------------- cards (same look as the video) ----------------
def lobby_caption(L) -> str:
    pot, tax, prize = split_pot(L["bet"], L["kind"])
    names = [n for _, n in L["players"]]
    nxt = {1: "دوم", 2: "سوم"}.get(len(names), "بعدی")
    lines = [
        f"**•━ Vip {BRAND} ━•**",
        "🎮 • بازی جدید •",
        "••• سازنده :",
        names[0],
        "🦁 تعداد بازیکنان :",
        str(L["kind"]),
        "💎 مقدار :",
        f"{money(L['bet'])} 💎",
        "👾 جایزه برنده :",
        f"{money(prize)} 💎",
        "⊖ مالیات :",
        f"{money(tax)} 💎",
    ]
    if len(names) > 1:
        lines += ["", "👥 بازیکنان: " + " ، ".join(names)]
    lines += ["", f"**در انتظار پیوستن بازیکن {nxt} •••**"]
    return "\n".join(lines)


def lobby_buttons(lid, L):
    rows = [[Button.inline("🔴 لغو بازی", f"lc:{lid}".encode()),
             Button.inline("✅ پیوستن", f"lj:{lid}".encode())]]
    if L["kind"] == 2:
        rows.append([Button.inline("🤖 بازی با ربات", f"lb:{lid}".encode())])
    return rows


def card_text(winner: str, losers: list, extra: str = "") -> str:
    t = "**شرط تمام شد.**\n"
    if extra:
        t += f"\n{extra}\n"
    return t + f"\nبرنده: {winner}\nبازنده: {' و '.join(losers)}"


def card_buttons(prize: int, bal_txt: str):
    return [[Button.inline("🟢 جایزه برنده", f"gi:p:{prize}".encode()),
             Button.inline(f"🟠 {money(prize)} الماس", f"gi:p:{prize}".encode())],
            [Button.inline("🔴 موجودی شما", b"gi:b"),
             Button.inline(f"🟠 {bal_txt} الماس", b"gi:b")]]


async def recover(bot):
    """After a restart: refund every bet locked in an open lobby."""
    for r in await db.lobby_all():
        for uid in r["players"]:
            await db.add(uid, r["bet"], "game_refund_restart")
        await db.lobby_delete(r["id"])


def register(bot, admin_ids):
    async def game_chat():
        return int(await db.get("game_group", os.getenv("GAME_GROUP_ID", "0")) or 0)

    async def send_card(e, text, buttons):
        m = await e.reply(text, file=await assets.banner(), buttons=buttons)
        assets.remember(m)
        return m

    # ------------------------------------------------------------ lobby games (video flow)
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
            open_by_user.pop(L["creator"], None)
            await db.lobby_delete(lid)
        try:
            await bot.edit_message(L["chat"], L["msg"], f"**{reason}**", buttons=None)
        except Exception:
            pass

    async def lobby_timeout(lid):
        await asyncio.sleep(LOBBY_TIMEOUT)
        await cancel_lobby(lid, "⌛ زمان بازی تموم شد؛ شرط‌ها برگشت داده شد.")

    async def run_lobby(lid):
        L = lobbies[lid]
        try:
            players = L["players"]
            winner = rng.choice(players)          # equal chance for everybody (bot included)
            _, _, prize = split_pot(L["bet"], L["kind"])
            if winner[0]:
                await db.add(winner[0], prize, "game_win")
            losers = [n for (p, n) in players if (p, n) != winner]
            bal = db.fmt(L["creator"], await db.balance(L["creator"]))
            text = card_text(winner[1], losers)
            btns = card_buttons(prize, bal)
            try:
                await bot.edit_message(L["chat"], L["msg"], text, buttons=btns)
            except Exception:
                m = await bot.send_message(L["chat"], text, file=await assets.banner(), buttons=btns)
                assets.remember(m)
        except Exception:
            log.exception("lobby game failed")
            for u, _ in L["players"]:
                if u:
                    await db.add(u, L["bet"], "game_refund_error")
            try:
                await bot.send_message(L["chat"], "⚠️ خطا توی بازی؛ شرط‌ها برگشت داده شد.")
            except Exception:
                pass
        finally:
            lobbies.pop(lid, None)
            open_by_user.pop(L["creator"], None)
            await db.lobby_delete(lid)

    async def cmd_game(e, uid, name, arg):
        kind, bet = parse_game(arg)
        if bet is None:
            return await e.reply("مثال: `بازی 50` (دونفره) یا `بازی 3 50` (سه‌نفره)")
        if bet < MIN_BET or bet > MAX_BET:
            return await e.reply(f"شرط باید بین {MIN_BET} تا {money(MAX_BET)} 💎 باشه")
        if uid in open_by_user:
            return await e.reply("تو یه بازی باز داری؛ اول اون تموم یا لغو بشه.")
        if await db.try_spend(uid, bet, "game_bet") is None:
            return await e.reply("💎 موجودی کافی نیست")
        lid = await db.lobby_create(e.chat_id, bet, kind, uid)
        L = {"chat": e.chat_id, "msg": 0, "bet": bet, "kind": kind, "creator": uid,
             "players": [(uid, name)], "lock": asyncio.Lock(), "state": "open"}
        try:
            m = await send_card(e, lobby_caption(L), lobby_buttons(lid, L))
        except Exception:
            log.exception("lobby send failed")
            await db.add(uid, bet, "game_refund_error")
            await db.lobby_delete(lid)
            return
        L["msg"] = m.id
        lobbies[lid] = L
        open_by_user[uid] = lid
        L["task"] = asyncio.create_task(lobby_timeout(lid))

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
        if start:
            L["task"].cancel()
            await e.answer()
            return await run_lobby(lid)
        await e.answer("پیوستی ✅")
        await e.edit(lobby_caption(L), buttons=lobby_buttons(lid, L))

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
        await e.answer()
        await run_lobby(lid)

    @bot.on(events.CallbackQuery(pattern=rb"^lc:"))
    async def cb_cancel(e):
        lid = int(e.data.decode().split(":")[1])
        L = lobbies.get(lid)
        if not L:
            return await e.answer("این بازی تموم شده", alert=True)
        if e.sender_id != L["creator"] and e.sender_id not in admin_ids:
            return await e.answer("فقط سازنده می‌تونه لغو کنه", alert=True)
        L["task"].cancel()
        await cancel_lobby(lid, "🔴 بازی لغو شد؛ شرط‌ها برگشت داده شد.")
        await e.answer("لغو شد")

    @bot.on(events.CallbackQuery(pattern=rb"^gi:"))
    async def cb_info(e):
        p = e.data.decode().split(":")
        if p[1] == "p":
            return await e.answer(f"🏆 جایزه‌ی برنده: {money(int(p[2]))} 💎", alert=True)
        bal = db.fmt(e.sender_id, await db.balance(e.sender_id))
        await e.answer(f"💎 موجودی شما: {bal}", alert=True)

    # ------------------------------------------------------------ duels vs bot (Telegram dice, tie = roll again)
    async def cmd_duel(e, uid, name, arg, kind):
        bet = parse_bet(arg.split()[0] if arg.split() else None)
        emoji = DUELS[kind]
        if bet is None or bet < MIN_BET or bet > MAX_BET:
            return await e.reply(f"مثال: `{ {'dice': 'تاس', 'dart': 'دارت', 'bowl': 'بولینگ'}[kind] } 50` "
                                 f"({MIN_BET} تا {money(MAX_BET)} 💎)")
        if await db.try_spend(uid, bet, f"{kind}_bet") is None:
            return await e.reply("💎 موجودی کافی نیست")
        try:
            a = b = 0
            for _ in range(3):
                m1 = await bot.send_message(e.chat_id, file=types.InputMediaDice(emoji), reply_to=e.id)
                await asyncio.sleep(3.5)
                m2 = await bot.send_message(e.chat_id, file=types.InputMediaDice(emoji))
                await asyncio.sleep(3.5)
                a, b = m1.media.value, m2.media.value
                if a != b:
                    break
            if a == b:
                await db.add(uid, bet, f"{kind}_refund")
                return await e.reply("🤝 چند بار مساوی شد؛ شرطت برگشت داده شد.")
            _, _, prize = split_pot(bet, 2)
            win = a > b
            if win:
                await db.add(uid, prize, f"{kind}_win")
            bal = db.fmt(uid, await db.balance(uid))
            text = card_text(name if win else "ربات", ["ربات" if win else name],
                             extra=f"{emoji} {name}: {a} | ربات: {b}")
            await send_card(e, text, card_buttons(prize, bal))
        except Exception:
            log.exception("duel failed")
            await db.add(uid, bet, f"{kind}_refund_error")
            await e.reply("⚠️ خطا؛ شرطت برگشت داده شد.")

    # ------------------------------------------------------------ coin / roulette (choose a side, 50/50)
    async def cmd_pick(e, uid, name, arg, kind):
        bet = parse_bet(arg.split()[0] if arg.split() else None)
        word = "سکه" if kind == "coin" else "رولت"
        if bet is None or bet < MIN_BET or bet > MAX_BET:
            return await e.reply(f"مثال: `{word} 50` ({MIN_BET} تا {money(MAX_BET)} 💎)")
        if await db.balance(uid) < bet:
            return await e.reply("💎 موجودی کافی نیست")
        _, tax, prize = split_pot(bet, 2)
        if kind == "coin":
            opts = [("🦁 شیر", "h"), ("🪙 خط", "t")]
            head = "🪙 **شیر یا خط**"
        else:
            opts = [("🔴 قرمز", "r"), ("⚫ مشکی", "b")]
            head = "🎡 **رولت** (۱۸ قرمز / ۱۸ مشکی)"
        text = (f"**•━ Vip {BRAND} ━•**\n{head}\n\n"
                f"بازیکن: {name}\n💎 شرط: {money(bet)}\n👾 جایزه: {money(prize)} 💎\n⊖ مالیات: {money(tax)} 💎\n\n"
                f"**یکی رو انتخاب کن 👇**")
        prefix = "co" if kind == "coin" else "ro"
        btns = [[Button.inline(t, f"{prefix}:{uid}:{bet}:{k}".encode()) for t, k in opts]]
        await send_card(e, text, btns)

    async def pick_result(e, kind):
        _, u, bet, side = e.data.decode().split(":")
        u, bet = int(u), int(bet)
        if e.sender_id != u:
            return await e.answer("این بازی مال تو نیست 😉", alert=True)
        key = (e.chat_id, e.message_id)
        if key in _busy:
            return await e.answer()
        _busy.add(key)
        try:
            if await db.try_spend(u, bet, f"{kind}_bet") is None:
                return await e.answer("💎 موجودی کافی نیست", alert=True)
            await e.answer()
            name = name_of(await e.get_sender())
            _, _, prize = split_pot(bet, 2)
            if kind == "coin":
                res = "h" if rng.random() < 0.5 else "t"
                label = {"h": "🦁 شیر", "t": "🪙 خط"}
                extra = f"انتخاب {name}: {label[side]}\nنتیجه: {label[res]}"
            else:
                await e.edit("🎡 **در حال چرخش...**", buttons=None)
                await asyncio.sleep(1.5)
                n = rng.randint(1, 36)
                res = "r" if n in RED else "b"
                label = {"r": "🔴 قرمز", "b": "⚫ مشکی"}
                extra = f"انتخاب {name}: {label[side]}\nنتیجه: {n} {label[res]}"
            win = res == side
            if win:
                await db.add(u, prize, f"{kind}_win")
            bal = db.fmt(u, await db.balance(u))
            text = card_text(name if win else "ربات", ["ربات" if win else name], extra=extra)
            await e.edit(text, buttons=card_buttons(prize, bal))
        except Exception:
            log.exception("%s result failed", kind)
        finally:
            _busy.discard(key)

    @bot.on(events.CallbackQuery(pattern=rb"^co:"))
    async def cb_coin(e):
        await pick_result(e, "coin")

    @bot.on(events.CallbackQuery(pattern=rb"^ro:"))
    async def cb_roul(e):
        await pick_result(e, "roul")

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

        cmd = EN.get(first[1:].split("@")[0].lower()) if first.startswith("/") else FA.get(first)
        if not cmd:
            return
        if e.chat_id != await game_chat():
            return  # games exist only in the special group
        if time.time() - _cool.get(e.sender_id, 0) < 1.2:
            return
        _cool[e.sender_id] = time.time()

        uid = e.sender_id
        s = await e.get_sender()
        if getattr(s, "bot", False):
            return
        name = name_of(s)
        await db.ensure_wallet(uid, name)

        if cmd == "game":
            return await cmd_game(e, uid, name, arg)
        if cmd in DUELS:
            return await cmd_duel(e, uid, name, arg, cmd)
        if cmd in ("coin", "roul"):
            return await cmd_pick(e, uid, name, arg, cmd)
        if cmd == "help":
            return await e.reply(GAME_HELP)

        if cmd == "bal":
            r = await e.get_reply_message() if e.is_reply else None
            if r and r.sender_id:
                tid, tname = r.sender_id, name_of(await r.get_sender())
            else:
                tid, tname = uid, name
            return await e.reply(f"💎 موجودی {mention(tid, tname)}: **{db.fmt(tid, await db.balance(tid))}**")

        if cmd == "daily":
            bal, wait = await db.claim_daily(uid, name)
            if bal is None:
                return await e.reply(f"⏳ {wait // 3600} ساعت و {wait % 3600 // 60} دقیقه دیگه")
            return await e.reply(f"🎁 {db.DAILY_BONUS} 💎 گرفتی! موجودی: {db.fmt(uid, bal)}")

        if cmd == "top":
            rows = await db.top(10)
            lines = [f"{i}. {r['name'] or r['user_id']} — {money(r['diamonds'])} 💎" for i, r in enumerate(rows, 1)]
            return await e.reply("🏆 **برترین‌ها**\n" + ("\n".join(lines) or "هنوز کسی نیست"))

        if cmd == "give":
            r = await e.get_reply_message() if e.is_reply else None
            amt = parse_bet(arg.split()[0] if arg.split() else None)
            if not r or not r.sender_id or not amt or amt <= 0 or r.sender_id == uid:
                return await e.reply("روی پیام طرف ریپلای کن و بنویس: `انتقال 25`")
            rs = await r.get_sender()
            if getattr(rs, "bot", False):
                return await e.reply("به ربات نمیشه الماس داد 😅")
            if await db.try_spend(uid, amt, "transfer_out") is None:
                return await e.reply("💎 موجودی کافی نیست")
            rname = name_of(rs)
            await db.add(r.sender_id, amt, "transfer_in", name=rname)
            return await e.reply(f"✅ {mention(uid, name)} تعداد **{money(amt)}** 💎 به {mention(r.sender_id, rname)} انتقال داد.")
