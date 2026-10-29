"""Diamond games (all commands in Persian), played ONLY in the special game group. Runs on the manager bot.

Games (all FAIR - every player has exactly the same chance):
  بازی [مبلغ]    duel: everybody rolls a real Telegram dice, highest wins (ties re-roll)
  کازینو [مبلغ]  fair roulette table (2-6 seats, provably-fair draw)
  تاس [مبلغ]     dice duel vs the bot: best of 3 hands, two dice each, no tax
Commands (game group):  بازی [مبلغ]  |  کازینو [مبلغ]  |  تاس [مبلغ]  |  بازی ها  |  راهنما
Everywhere:             موجودی  |  انتقال [مبلغ] (ریپلای)  |  برترین
Admin:                  تنظیم بازی

Every game opens a button panel (banner + coloured buttons): entry amount, players, start.
"""
import asyncio
import hashlib
import html
import itertools
import logging
import os
import random
import re
import secrets
import time

from telethon import events, types

import botapi
import db
import utils
from botapi import GREEN, RED, BLUE, btn, kb

log = logging.getLogger("games")

MIN_GAME = 20                                              # minimum entry for EVERY game
MIN_SOLO = 20
HARD_CAP = 10 ** 9                                         # "unlimited" (only keeps numbers inside BIGINT range)
MAX_GAME = int(os.getenv("MAX_GAME_BET", "0")) or HARD_CAP   # 0 / unset = no limit
MAX_SOLO = int(os.getenv("MAX_SOLO_BET", "0")) or HARD_CAP
FEE = int(os.getenv("GAME_FEE_PERCENT", "5"))              # duel tax: pot 88 -> tax 4 -> prize 84
BRAND = os.getenv("BRAND", "Vip MMD")
_rng = random.SystemRandom()
LOBBY_TIMEOUT = 180
PANEL_TIMEOUT = 240
MAX_OPEN_PER_USER = 2
MAX_TABLE_SEATS = 6

lobbies: dict[int, dict] = {}            # open duels / casino tables (id = db lobby id)
panels: dict[int, dict] = {}             # setup panels (id = counter)
awaiting: dict[tuple, tuple] = {}        # (chat, user) -> (panel id, deadline)   custom amount typing
_cool: dict[int, float] = {}
_ids = itertools.count(int(time.time()) % 10 ** 8)

DIE = "⚀⚁⚂⚃⚄⚅"
GAME_TITLE = {"duel": "بازی دو/سه‌نفره 🎲", "casino": "کازینو — رولت عادلانه 🎡", "dice": "دوئل تاس 🎲"}


def nf(s):
    """Normalise a Persian command word (ي/ك, ZWNJ, underscores)."""
    return re.sub(r"[_\u200c]", "", (s or "").replace("ي", "ی").replace("ك", "ک")).lower()


def mention(uid, name):
    return f"[{utils.clean_name(name)}](tg://user?id={uid})"


def name_of(u):
    return utils.clean_name(getattr(u, "first_name", None) or getattr(u, "username", None) or str(getattr(u, "id", "")))


def esc(n):
    return html.escape(utils.clean_name(n))


def limit_txt(hi):
    return "بدون سقف" if hi >= HARD_CAP else f"حداکثر {hi:,} الماس"


def mult_txt(m):
    return f"{m:g}"


async def recover(bot):
    """After a crash/restart: refund every bet that was locked in an open lobby / table."""
    for r in await db.lobby_all():
        for uid in r["players"]:
            await db.add(uid, r["bet"], "game_refund_restart")
        await db.lobby_delete(r["id"])


HELP = (f"<b><i>• — {html.escape(BRAND)} — •</i></b>\n\n<b>🎮 راهنمای بازی‌ها</b> (فقط توی گپ مخصوص بازی)\n"
        "⚖️ <b>همه‌ی بازی‌ها عادلانه‌ان:</b> شانس همه‌ی بازیکن‌ها دقیقاً برابره.\n\n"
        "🎲 <code>بازی 20</code> — دو/سه‌نفره؛ همه تاس می‌ندازن، تاس بزرگ‌تر می‌بره (مساوی = دوباره). با ربات هم میشه\n"
        "🎡 <code>کازینو 50</code> — رولت عادلانه: ۲ تا ۶ صندلی، برنده با قرعه‌ی قابل اثبات (Provably Fair) مشخص میشه\n"
        "🎲 <code>تاس 20</code> — دوئل تاس با ربات: سه دست، دو تاس هر نفر، اول کسی که ۲ دست ببره برنده‌ست (×۲، بدون مالیات)\n"
        "📋 <code>بازی ها</code> — منوی بازی‌ها\n\n"
        "💎 <code>موجودی</code> | <code>برترین</code> | <code>انتقال 25</code> (ریپلای)\n"
        "مبلغ رو می‌تونی بعد از اسم بازی بنویسی: <code>کازینو 50</code>")


def register(bot, admin_ids):
    async def game_chat():
        return int(await db.get("game_group", os.getenv("GAME_GROUP_ID", "0")) or 0)

    # ================================================================ shared helpers
    def clamp(game, bet):
        lo, hi = (MIN_GAME, MAX_GAME) if game in ("duel", "casino") else (MIN_SOLO, MAX_SOLO)
        return max(lo, min(hi, bet)), lo, hi

    def open_count(uid):
        return sum(1 for L in lobbies.values() if L["creator"] == uid)

    def bet_head(P):
        return (f"<b><i>• — {html.escape(BRAND)} — •</i></b>\n\n<b>• {GAME_TITLE[P['game']]}</b>\n"
                f"👤 سازنده: {esc(P['name'])}\n💎 مبلغ ورودی: <b>{P['bet']:,} الماس</b>\n")

    # ---------------------------------------------------------------- duel economics
    def pot_of(L):
        return L["bet"] * L["kind"]

    def tax_of(L):
        return pot_of(L) * FEE // 100

    def prize_of(L):
        return pot_of(L) - tax_of(L)

    # ================================================================ panel rendering
    def panel_view(P):
        g, v, bet = P["game"], P["view"], P["bet"]
        _, lo, hi = clamp(g, bet)
        if v == "bet":
            t = (bet_head(P) + f"\n💎 <b>مبلغ ورودی رو انتخاب کن</b> (مثلاً 20 الماس)\nحداقل {lo:,} الماس — {limit_txt(hi)}")
            presets = [x for x in (20, 50, 100, 500, 1000, 5000) if lo <= x <= hi][:6]
            rows = [[btn(f"{x:,} 💎", f"gp:{P['id']}:s:{x}", GREEN) for x in presets[i:i + 3]] for i in range(0, len(presets), 3)]
            rows.append([btn("✏️ مبلغ دلخواه", f"gp:{P['id']}:bc", BLUE), btn("↩️ برگشت", f"gp:{P['id']}:m", RED)])
            return t, kb(rows)
        if v == "custom":
            return (bet_head(P) + f"\n✏️ <b>مبلغ دلخواهت رو همین‌جا توی گروه بفرست</b>\nحداقل {lo:,} الماس — {limit_txt(hi)} (تا ۶۰ ثانیه)",
                    kb([[btn("↩️ برگشت", f"gp:{P['id']}:m", RED)]]))
        if v == "n":
            t = bet_head(P) + "\n👥 <b>تعداد صندلی‌های میز رو انتخاب کن</b>\n(همه‌ی صندلی‌ها شانس برابر دارن)"
            rows = [[btn(("✅ " if P["n"] == i else "") + f"{i} نفر", f"gp:{P['id']}:sn:{i}", GREEN) for i in grp]
                    for grp in ((2, 3, 4), (5, 6))]
            rows.append([btn("↩️ برگشت", f"gp:{P['id']}:m", RED)])
            return t, kb(rows)
        # ---- main views
        pid = P["id"]
        bet_btn = btn(f"💎 تعیین مبلغ ورودی · {bet:,} الماس", f"gp:{pid}:b", BLUE)
        cancel = btn("❌ انصراف", f"gp:{pid}:x", RED)
        if g == "duel":
            t = (bet_head(P) + "\n<b>نوع بازی رو انتخاب کن 👇</b>\n"
                 f"👥 دونفره: جایزه {bet * 2 - bet * 2 * FEE // 100:,} 💎 (می‌تونی با ربات هم بازی کنی 🤖)\n"
                 f"👥 سه‌نفره: جایزه {bet * 3 - bet * 3 * FEE // 100:,} 💎\n"
                 f"➖ مالیات: {FEE}٪")
            return t, kb([[btn("👥 دو‌نفره", f"gp:{pid}:d2", GREEN), btn("👥👥 سه‌نفره", f"gp:{pid}:d3", GREEN)],
                          [bet_btn], [cancel]])
        if g == "casino":
            n = P["n"]
            pot = bet * n
            t = (bet_head(P) + f"🪑 تعداد صندلی: <b>{n}</b>\n\n<b>🎡 رولت عادلانه — قوانین</b>\n"
                 f"• همه‌ی صندلی‌ها شانس <b>دقیقاً برابر</b> دارن (۱ از {n})\n"
                 "• برنده با چرخ قرعه‌کشی مشخص میشه؛ کد تعهد (Hash) قبل از شروع قفل و بعد از بازی باز میشه، پس هر کسی می‌تونه صحتش رو چک کنه\n"
                 f"• 🏆 جایزه: <b>{pot - pot * FEE // 100:,}</b> 💎 | ➖ مالیات {FEE}٪ (فقط از جایزه)\n"
                 "• اگه بازیکن کم بود، سازنده می‌تونه با 🤖 ربات پر کنه (شانس ربات هم برابره)")
            return t, kb([[bet_btn], [btn(f"🪑 تعداد صندلی · {n} نفر", f"gp:{pid}:n", BLUE)],
                          [btn("🎡 ساخت میز", f"gp:{pid}:go", GREEN)], [cancel]])
        # dice duel
        t = (bet_head(P) + "\n<b>🎲 دوئل تاس با ربات</b>\n"
             "• سه دست؛ توی هر دست تو و ربات <b>دو تاس</b> می‌ندازین و مجموع بیشتر دست رو می‌بره\n"
             "• دست مساوی دوباره تکرار میشه\n• اول کسی که ۲ دست ببره برنده‌ست\n"
             f"• شانس هر دو طرف دقیقاً ۵۰٪ | 🏆 برد: <b>×2</b> ({bet * 2:,} 💎) | بدون مالیات")
        return t, kb([[bet_btn], [btn("🎲 شروع دوئل!", f"gp:{pid}:go", GREEN)], [cancel]])

    async def render(P):
        text, markup = panel_view(P)
        try:
            await botapi.edit_banner(P["chat"], P["msg"], text, markup)
        except Exception:
            log.exception("panel edit failed")

    def touch(P):
        if P.get("task"):
            P["task"].cancel()
        P["task"] = asyncio.create_task(panel_expire(P["id"]))

    async def panel_expire(pid):
        await asyncio.sleep(PANEL_TIMEOUT)
        P = panels.get(pid)
        if P and P["state"] in ("setup", "done"):
            panels.pop(pid, None)
            awaiting.pop((P["chat"], P["owner"]), None)
            try:
                await botapi.edit_banner(P["chat"], P["msg"], "⌛ این پنل منقضی شد؛ دوباره دستور رو بفرست.", kb([]))
            except Exception:
                pass

    async def open_panel(chat, reply_to, uid, name, game, bet=None, n=None):
        b, _, _ = clamp(game, bet or 20)
        P = {"id": next(_ids), "chat": chat, "msg": 0, "owner": uid, "name": name, "game": game, "bet": b,
             "n": n or (3 if game == "casino" else 1), "view": "main", "state": "setup", "lock": asyncio.Lock()}
        text, markup = panel_view(P)
        m = await botapi.send_banner(chat, text, markup, reply_to=reply_to)
        P["msg"] = m["message_id"]
        panels[P["id"]] = P
        touch(P)
        return P

    def again_markup(P, extra=None):
        pid = P["id"]
        rows = [[btn("🔁 دوباره (همین مبلغ)", f"gp:{pid}:go", GREEN), btn("⚙️ تنظیمات", f"gp:{pid}:m", BLUE)],
                [btn("❌ بستن", f"gp:{pid}:x", RED)]]
        return kb((extra or []) + rows)

    # ================================================================ lobbies (duel + casino table)
    def lobby_text(L, note=None):
        creator = L["players"][0][1]
        names = "، ".join(esc(n) for _, n in L["players"])
        if L["type"] == "duel":
            t = (f"<b><i>• — {html.escape(BRAND)} — •</i></b>\n\n<b>• بازی جدید 🎲</b>\n"
                 f"👤 سازنده:\n{esc(creator)}\n\n👥 تعداد بازیکنان:\n{L['kind']} 🇮🇷\n\n💎 مقدار:\n{L['bet']} 💎\n\n"
                 f"🏆 جایزه برنده:\n{prize_of(L)} 💎\n\n➖ مالیات:\n{tax_of(L)} 💎\n")
            if len(L["players"]) > 1:
                t += f"\n🙋 پیوسته‌ها: {names}\n"
            return t + (f"\nدر انتظار بازیکن {len(L['players']) + 1} از {L['kind']} •••" if note is None else note)
        seats = "\n".join(f"{i + 1}. " + (esc(L["players"][i][1]) if i < len(L["players"]) else "⏳ <i>خالی</i>")
                          for i in range(L["n"]))
        t = (f"<b><i>• — {html.escape(BRAND)} — •</i></b>\n\n<b>• 🎡 رولت عادلانه</b>\n"
             f"👤 سازنده: {esc(creator)}\n💎 ورودی هر نفر: <b>{L['bet']:,} الماس</b>\n"
             f"🏆 جایزه (میز پر): <b>{prize_of(L):,}</b> 💎 | ➖ مالیات {FEE}٪\n\n"
             f"🪑 <b>صندلی‌ها</b>\n{seats}\n\n"
             f"⚖️ شانس همه‌ی صندلی‌ها دقیقاً برابره.\n"
             f"🔐 کد تعهد قرعه: <code>{L['commit'][:24]}…</code>\n")
        return t + ("\nدر انتظار بازیکن‌ها •••" if note is None else note)

    def lobby_markup(lid, L):
        if L["type"] == "duel":
            rows = [[btn("پیوستن ✅", f"lj:{lid}", GREEN), btn("لغو بازی ❌", f"lc:{lid}", RED)]]
            if L["kind"] == 2:
                rows.append([btn("بازی با ربات 🤖", f"lb:{lid}", BLUE)])
            return kb(rows)
        rows = [[btn(f"🪑 نشستن پای میز ({L['bet']:,} 💎)", f"lj:{lid}", GREEN)],
                [btn("▶️ شروع (سازنده)", f"ls:{lid}", BLUE), btn("🤖 پر کردن با ربات", f"lb:{lid}", BLUE)],
                [btn("لغو ❌", f"lc:{lid}", RED)]]
        return kb(rows)

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
        await edit_lobby(L, reason, kb([]))

    async def lobby_timeout(lid):
        await asyncio.sleep(LOBBY_TIMEOUT)
        await cancel_lobby(lid, "⌛ زمان تموم شد؛ شرط‌ها برگشت داده شد.")

    async def new_lobby(P, kind, ltype):
        """Panel -> lobby on the same message. The creator pays the entry now. Returns (error_text|None, lobby_id)."""
        uid = P["owner"]
        if open_count(uid) >= MAX_OPEN_PER_USER:
            return "تو همین الان چند بازی/میز باز داری؛ اول اونا تموم بشن.", None
        if await db.try_spend(uid, P["bet"], "game_bet" if ltype == "duel" else "casino_bet") is None:
            return "💎 موجودی کافی نیست", None
        lid = await db.lobby_create(P["chat"], P["bet"], kind if ltype == "duel" else 100 + kind, uid)
        L = {"type": ltype, "chat": P["chat"], "msg": P["msg"], "bet": P["bet"], "kind": kind, "n": kind,
             "creator": uid, "players": [(uid, P["name"])], "lock": asyncio.Lock(), "state": "open"}
        if ltype == "casino":                                  # commit-reveal: hash is shown now, seed after the spin
            L["seed"] = secrets.token_hex(16)
            L["commit"] = hashlib.sha256(L["seed"].encode()).hexdigest()
        lobbies[lid] = L
        L["task"] = asyncio.create_task(lobby_timeout(lid))
        panels.pop(P["id"], None)
        if P.get("task"):
            P["task"].cancel()
        await edit_lobby(L, lobby_text(L), lobby_markup(lid, L))
        return None, lid

    async def roll(chat, emoji, reply_to, wait):
        m = await bot.send_message(chat, file=types.InputMediaDice(emoji), reply_to=reply_to)
        await asyncio.sleep(wait)
        v = getattr(getattr(m, "media", None), "value", None)
        return v if isinstance(v, int) and v > 0 else (_rng.randint(1, 64) if emoji == "🎰" else _rng.randint(1, 6))

    async def run_duel(lid):
        """Everyone rolls a real Telegram dice; highest wins (ties: only the tied players re-roll)."""
        L = lobbies[lid]
        try:
            players = L["players"]
            rolls = [[] for _ in players]
            order = list(range(len(players)))
            for _rnd in range(4):
                vals = {}
                for i in order:
                    live = lobby_text(L, "")
                    lines = "\n".join(f"🎲 {esc(players[k][1])}: " + (" ← ".join(map(str, rolls[k])) or "—") for k in range(len(players)))
                    await edit_lobby(L, f"{live}\n\n{lines}\n\n⏳ نوبت <b>{esc(players[i][1])}</b> ...", kb([]))
                    v = await roll(L["chat"], "🎲", L["msg"], 3.6)
                    rolls[i].append(v)
                    vals[i] = v
                top = max(vals.values())
                order = [i for i, v in vals.items() if v == top]
                if len(order) == 1:
                    break
            win = order[0] if len(order) == 1 else _rng.choice(order)
            wid, wname = players[win]
            prize = prize_of(L)
            if wid:
                await db.add(wid, prize, "game_win")
            lines = "\n".join(f"{'🏆' if k == win else '▫️'} {esc(players[k][1])}: " + " ← ".join(map(str, rolls[k]))
                              for k in range(len(players)))
            text = (f"<b><i>• — {html.escape(BRAND)} — •</i></b>\n\n<b>🎲 شرط تمام شد</b>\n\n{lines}\n\n"
                    f"🏆 برنده: <b>{esc(wname)}</b>\n💎 جایزه: <b>{prize:,}</b> | ➖ مالیات: {tax_of(L)}")
            await edit_lobby(L, text, kb([[btn(f"🔁 بازی جدید ({L['bet']} 💎)", f"gr:duel:{L['bet']}:{L['kind']}", GREEN)]]))
        except Exception:
            log.exception("duel failed, refunding")
            for u, _ in L["players"]:
                if u:
                    await db.add(u, L["bet"], "game_refund_error")
            await edit_lobby(L, "⚠️ خطا توی بازی؛ شرط‌ها برگشت داده شد.", kb([]))
        finally:
            lobbies.pop(lid, None)
            await db.lobby_delete(lid)

    async def run_table(lid):
        """Fair roulette: every seat has exactly 1/N chance. Winner = SHA256(seed:spin) mod N, where the seed's hash
        (commit) was shown BEFORE anybody sat down, so the result can be verified by anyone afterwards."""
        L = lobbies[lid]
        paid = False
        try:
            players = L["players"]
            n = len(players)
            idx = int(hashlib.sha256((L["seed"] + ":spin").encode()).hexdigest(), 16) % n
            head = (f"<b><i>• — {html.escape(BRAND)} — •</i></b>\n\n<b>🎡 رولت عادلانه</b>\n"
                    f"💎 ورودی: {L['bet']:,} | 🏆 جایزه: <b>{prize_of(L):,}</b>\n\n")

            def wheel(pos):
                return "\n".join(f"{'👉' if i == pos else '▫️'} {i + 1}. {esc(nm)}" + ("  ◀️" if i == pos else "")
                                 for i, (_, nm) in enumerate(players))

            frames = list(range(2 * n + idx + 1))[-9:]             # the last step always lands on `idx`
            for k, st in enumerate(frames):
                await edit_lobby(L, head + wheel(st % n) + "\n\n🌀 چرخ داره می‌چرخه ...", kb([]))
                await asyncio.sleep(0.6 + 0.13 * k)
            wid, wname = players[idx]
            prize = prize_of(L)
            if wid:
                await db.add(wid, prize, "casino_win")
            paid = True
            text = (head + wheel(idx) + f"\n\n🏆 <b>برنده: {esc(wname)}</b> (صندلی {idx + 1})\n"
                    f"💎 جایزه: <b>{prize:,}</b> | ➖ مالیات: {tax_of(L):,}\n\n"
                    f"🔐 <b>اثبات عادلانه بودن</b>\nseed: <code>{L['seed']}</code>\n"
                    f"SHA256(seed) = <code>{L['commit'][:24]}…</code> ← همونیه که قبل از شروع دیدی\n"
                    f"برنده = SHA256(seed + \":spin\") mod {n} → صندلی {idx + 1}")
            await edit_lobby(L, text, kb([[btn(f"🔁 میز جدید ({L['bet']} 💎)", f"gr:casino:{L['bet']}:{L['n']}", GREEN)]]))
        except Exception:
            log.exception("table failed")
            if not paid:
                for u, _ in L["players"]:
                    if u:
                        await db.add(u, L["bet"], "game_refund_error")
            await edit_lobby(L, "⚠️ خطا توی میز؛ شرط‌ها برگشت داده شد." if not paid else "⚠️ خطا بعد از پرداخت جایزه؛ نتیجه رو از مدیر بپرس.", kb([]))
        finally:
            lobbies.pop(lid, None)
            await db.lobby_delete(lid)

    async def start_lobby(lid):
        L = lobbies.get(lid)
        if not L:
            return
        L["state"] = "running"
        L["task"].cancel()
        await (run_duel(lid) if L["type"] == "duel" else run_table(lid))

    # ================================================================ solo games (against the house)
    async def finish(P, text, win_extra=None):
        P["state"] = "done"
        P["view"] = "main"
        touch(P)
        try:
            await botapi.edit_banner(P["chat"], P["msg"], text, again_markup(P, win_extra))
        except Exception:
            log.exception("result edit failed")

    async def play_solo(P):
        """🎲 Dice duel vs the bot - best of 3 hands, two dice each, ties replay. Both sides win with exactly 50%,
        payout x2 and no tax, so the expected value is exactly the entry fee (a perfectly fair game)."""
        bet, uid = P["bet"], P["owner"]
        head = (f"<b><i>• — {html.escape(BRAND)} — •</i></b>\n\n<b>• 🎲 دوئل تاس</b>\n"
                f"👤 {esc(P['name'])} | 💎 ورودی: {bet:,}\n\n")
        if await db.try_spend(uid, bet, "dice_bet") is None:
            return "💎 موجودی کافی نیست"
        P["state"] = "running"
        face = lambda v: DIE[v - 1]
        pips = lambda w: "🟢" * w + "⚪" * (2 - w)
        try:
            me = bot_ = 0
            hands = []
            while me < 2 and bot_ < 2 and len(hands) < 7:
                score = f"{esc(P['name'])} {pips(me)}   ⚔️   {pips(bot_)} 🤖\n\n"
                log_lines = "\n".join(hands)
                for _ in range(3):                                         # rolling animation
                    fa = " ".join(face(_rng.randint(1, 6)) for _ in range(2))
                    fb = " ".join(face(_rng.randint(1, 6)) for _ in range(2))
                    await botapi.edit_banner(P["chat"], P["msg"], head + score + (log_lines + "\n\n" if hands else "")
                                             + f"🌀 دست {len(hands) + 1}\n{fa}   |   {fb}")
                    await asyncio.sleep(0.8)
                a = [_rng.randint(1, 6), _rng.randint(1, 6)]
                b = [_rng.randint(1, 6), _rng.randint(1, 6)]
                sa, sb = sum(a), sum(b)
                tag = "✅" if sa > sb else ("❌" if sa < sb else "🤝 مساوی؛ دوباره")
                me += sa > sb
                bot_ += sb > sa
                hands.append(f"دست {len(hands) + 1}: {face(a[0])} {face(a[1])} = <b>{sa}</b>  vs  {face(b[0])} {face(b[1])} = <b>{sb}</b>  {tag}")
                await botapi.edit_banner(P["chat"], P["msg"], head + f"{esc(P['name'])} {pips(me)}   ⚔️   {pips(bot_)} 🤖\n\n"
                                         + "\n".join(hands))
                await asyncio.sleep(1.1)
            board = f"{esc(P['name'])} {pips(me)}   ⚔️   {pips(bot_)} 🤖\n\n" + "\n".join(hands) + "\n\n"
            if me >= 2:
                bal, line = await db.add(uid, bet * 2, "dice_win"), f"🏆 <b>بردی!</b> +{bet:,} 💎"
            elif bot_ >= 2:
                bal, line = await db.balance(uid), f"💥 باختی! -{bet:,} 💎"
            else:                                                          # practically impossible: refund
                bal, line = await db.add(uid, bet, "dice_tie"), "🤝 نتیجه مساوی شد؛ شرطت برگشت"
            await finish(P, head + board + line + f"\n\n💎 موجودی: <b>{db.fmt(bal)}</b>")
        except Exception:
            log.exception("dice failed, refunding")
            await db.add(uid, bet, "dice_refund")
            await finish(P, head + "⚠️ خطا توی بازی؛ شرطت برگشت داده شد.")
        return None

    # ================================================================ transfer: reply + "انتقال 25"
    async def do_give(e, uid, name, arg):
        a = utils.to_en_digits(arg).split()
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
        sb, _tb = res
        await e.reply(f"✅ **انتقال انجام شد**\n\n👤 از: {mention(uid, name)}\n🎁 به: {mention(r.sender_id, tname)}\n"
                      f"💎 مقدار: **{amt:,}**\n\nموجودی تو: {db.fmt(sb)}")

    # ================================================================ group messages
    CMD = {"موجودی": "bal", "برترین": "top", "برترینها": "top", "انتقال": "give", "بازی": "duel", "بازیها": "menu",
           "کازینو": "casino", "تاس": "dice",
           "راهنما": "help", "راهنمای": "help"}

    def parse(t):
        w = t.split()
        if not w:
            return None, ""
        if w[0].startswith("/"):                       # only the old admin command is kept (hidden alias)
            return ("setgame", "") if re.match(r"^/setgame(@\w+)?$", w[0]) else (None, "")
        n0, n1 = nf(w[0]), nf(w[1]) if len(w) > 1 else ""
        if n0 == "تنظیم" and n1 == "بازی":
            return "setgame", ""
        if n0 == "بازی" and n1 == "ها":
            return "menu", ""
        cmd = CMD.get(n0)
        if cmd in ("casino", "dice") and not (
                len(w) == 1 or (len(w) == 2 and w[1].isdigit())):
            return None, ""                           # «تاس خوبیه» is just chat, not a command
        return cmd, " ".join(w[1:])

    def menu_markup():
        return kb([[btn("🎲 بازی دو/سه‌نفره", "gm:duel", GREEN), btn("🎡 کازینو (رولت)", "gm:casino", GREEN)],
                   [btn("🎲 دوئل تاس با ربات", "gm:dice", BLUE)],
                   [btn("🏆 برترین‌ها", "gm:top", RED)]])

    MENU_TEXT = (f"<b><i>• — {html.escape(BRAND)} — •</i></b>\n\n<b>🎮 منوی بازی‌ها</b>\n"
                 "یک بازی رو بزن تا پنلش باز بشه 👇")

    @bot.on(events.NewMessage(func=lambda e: e.is_group and not e.out))
    async def on_msg(e):
        t = utils.to_en_digits((e.raw_text or "").strip())
        if not t or not e.sender_id:
            return

        # custom entry amount typed after pressing «✏️ مبلغ دلخواه»
        aw = awaiting.get((e.chat_id, e.sender_id))
        if aw and t.replace(",", "").isdigit():
            pid, until = aw
            if not await db.claim_event(f"m:{e.chat_id}:{e.id}"):
                return
            awaiting.pop((e.chat_id, e.sender_id), None)
            P = panels.get(pid)
            if P and time.time() < until and P["state"] == "setup":
                P["bet"], _, _ = clamp(P["game"], int(t.replace(",", "")))
                P["view"] = "main"
                touch(P)
                try:
                    await e.delete()
                except Exception:
                    pass
                return await render(P)

        cmd, arg = parse(t)
        if not cmd:
            return
        if not await db.claim_event(f"m:{e.chat_id}:{e.id}"):
            return                                    # already handled (duplicate update / second process)
        if cmd == "setgame":
            if e.sender_id not in admin_ids:
                return
            await db.set_("game_group", e.chat_id)
            return await e.reply("🎮 این گروه شد «گپ مخصوص بازی».")
        if cmd not in ("bal", "give") and e.chat_id != await game_chat():
            return                                    # games exist only in the special group
        if time.time() - _cool.get(e.sender_id, 0) < 1.5:
            return
        _cool[e.sender_id] = time.time()

        uid = e.sender_id
        name = name_of(await e.get_sender())
        await db.ensure_wallet(uid, name)

        if cmd == "bal":
            r = await e.get_reply_message() if e.is_reply else None
            if r and r.sender_id:
                tid, tname = r.sender_id, name_of(await r.get_sender())
            else:
                tid, tname = uid, name
            bal = await db.balance(tid)
            who = "" if tid == uid else f'<a href="tg://user?id={tid}">{html.escape(tname)}</a>'
            text, markup = botapi.balance_card(bal, db.fmt(bal).replace("♾ ", "").replace("نامحدود", "∞"), db.value_text(bal), who)
            try:
                return await botapi.send_banner(e.chat_id, text, markup, reply_to=e.id)
            except Exception:
                return await e.reply(f"💎 موجودی {mention(tid, tname)}: **{db.fmt(bal)}**\n💰 {db.value_text(bal)}")
        if cmd == "top":
            rows = await db.top(10)
            lines = [f"{i}. {utils.clean_name(r['name'] or str(r['user_id']))} — {r['diamonds']:,} 💎" for i, r in enumerate(rows, 1)]
            return await e.reply("🏆 **برترین‌ها**\n" + ("\n".join(lines) or "هنوز کسی نیست"))
        if cmd == "give":
            return await do_give(e, uid, name, arg)
        if cmd == "help":
            return await botapi.send_text(e.chat_id, HELP, None, reply_to=e.id)
        if cmd == "menu":
            return await botapi.send_banner(e.chat_id, MENU_TEXT, menu_markup(), reply_to=e.id)

        a = arg.split()
        bet = int(a[0]) if a and a[0].isdigit() else None
        game, n = cmd, None
        if bet is not None:
            _, lo, hi = clamp(game, bet)
            if not lo <= bet <= hi:
                return await e.reply(f"مبلغ ورودی حداقل {lo:,} 💎 باشه" + ("" if hi >= HARD_CAP else f" و حداکثر {hi:,}"))
        try:
            P = await open_panel(e.chat_id, e.id, uid, name, game, bet, n)
        except Exception:
            log.exception("open panel failed")
            return await e.reply("⚠️ باز کردن پنل ناموفق بود.")
        if game == "duel" and len(a) > 1 and a[1] in ("2", "3"):          # «بازی 20 3» skips the question
            err, _ = await new_lobby(P, int(a[1]), "duel")
            if err:
                await e.reply(err)

    # ================================================================ callbacks
    @bot.on(events.CallbackQuery(pattern=rb"^gm:"))
    async def cb_menu(e):
        what = e.data.decode().split(":")[1]
        if what == "top":
            rows = await db.top(10)
            lines = [f"{i}. {utils.clean_name(r['name'] or str(r['user_id']))} — {r['diamonds']:,} 💎" for i, r in enumerate(rows, 1)]
            await e.answer()
            return await bot.send_message(e.chat_id, "🏆 **برترین‌ها**\n" + ("\n".join(lines) or "هنوز کسی نیست"))
        uid = e.sender_id
        name = name_of(await e.get_sender())
        await db.ensure_wallet(uid, name)
        if what not in GAME_TITLE:
            return await e.answer()
        await e.answer()
        await open_panel(e.chat_id, None, uid, name, what, None, None)

    @bot.on(events.CallbackQuery(pattern=rb"^gr:"))
    async def cb_replay(e):
        _, game, bet, n = e.data.decode().split(":")
        uid = e.sender_id
        name = name_of(await e.get_sender())
        await db.ensure_wallet(uid, name)
        await e.answer("🔁 پنل جدید برات باز شد")
        await open_panel(e.chat_id, None, uid, name, game, int(bet), int(n) if game == "casino" else None)

    @bot.on(events.CallbackQuery(pattern=rb"^gp:"))
    async def cb_panel(e):
        p = e.data.decode().split(":")
        pid, act, arg = int(p[1]), p[2], (p[3] if len(p) > 3 else "")
        P = panels.get(pid)
        if not P:
            return await e.answer("این پنل منقضی شده؛ دوباره دستور رو بفرست.", alert=True)
        if e.sender_id != P["owner"] and e.sender_id not in admin_ids:
            return await e.answer("این پنل مال تو نیست 😉 دستور خودت رو بفرست", alert=True)
        if P["state"] == "running":
            return await e.answer("⏳ بازی در حال انجامه ...")
        touch(P)
        g = P["game"]
        if act == "x":
            panels.pop(pid, None)
            awaiting.pop((P["chat"], P["owner"]), None)
            await e.answer("بسته شد")
            return await botapi.edit_banner(P["chat"], P["msg"], "❌ پنل بسته شد.", kb([]))
        if act == "m":
            P["view"] = "main"
            P["state"] = "setup"
            awaiting.pop((P["chat"], P["owner"]), None)
            await e.answer()
            return await render(P)
        if act == "b":
            P["view"] = "bet"
            await e.answer()
            return await render(P)
        if act == "bc":
            P["view"] = "custom"
            awaiting[(P["chat"], P["owner"])] = (pid, time.time() + 60)
            await e.answer("مبلغ رو توی گروه بفرست ✏️")
            return await render(P)
        if act == "s":
            P["bet"], _, _ = clamp(g, int(arg))
            P["view"] = "main"
            await e.answer(f"💎 مبلغ ورودی: {P['bet']:,} الماس")
            return await render(P)
        if act == "n":
            P["view"] = "n"
            await e.answer()
            return await render(P)
        if act == "sn":
            P["n"] = max(2, min(MAX_TABLE_SEATS, int(arg)))
            P["view"] = "main"
            await e.answer(f"👥 تعداد بازیکن: {P['n']} نفر")
            return await render(P)
        if act in ("d2", "d3"):
            async with P["lock"]:
                err, _ = await new_lobby(P, 2 if act == "d2" else 3, "duel")
            return await e.answer(err or ("👥 دونفره" if act == "d2" else "👥 سه‌نفره"), alert=bool(err))
        if act == "go":
            async with P["lock"]:
                if g == "casino":
                    err, _ = await new_lobby(P, P["n"], "casino")
                    return await e.answer(err or "🎡 میز ساخته شد", alert=bool(err))
                if P["owner"] not in db.ADMINS and await db.balance(P["owner"]) < P["bet"]:
                    return await e.answer("💎 موجودی کافی نیست", alert=True)
                await e.answer("🎲")
                err = await play_solo(P)
                if err:
                    await bot.send_message(P["chat"], err, reply_to=P["msg"])

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
            if await db.try_spend(uid, L["bet"], "game_bet" if L["type"] == "duel" else "casino_bet") is None:
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
            return await (run_duel(lid) if L["type"] == "duel" else run_table(lid))
        await e.answer("پیوستی ✅")
        await edit_lobby(L, lobby_text(L), lobby_markup(lid, L))

    @bot.on(events.CallbackQuery(pattern=rb"^ls:"))
    async def cb_start(e):
        lid = int(e.data.decode().split(":")[1])
        L = lobbies.get(lid)
        if not L:
            return await e.answer("این میز تموم شده", alert=True)
        if e.sender_id != L["creator"] and e.sender_id not in admin_ids:
            return await e.answer("فقط سازنده می‌تونه شروع کنه", alert=True)
        async with L["lock"]:
            if L["state"] != "open":
                return await e.answer("الان نمیشه", alert=True)
            if len(L["players"]) < 2:
                return await e.answer("حداقل ۲ نفر لازمه؛ یا با 🤖 ربات پرش کن", alert=True)
            L["state"] = "running"
            L["kind"] = L["n"] = len(L["players"])                       # pot = only the people who really sat
        await e.answer("🎡 شروع شد")
        await start_lobby(lid)

    @bot.on(events.CallbackQuery(pattern=rb"^lb:"))
    async def cb_bot(e):
        lid = int(e.data.decode().split(":")[1])
        L = lobbies.get(lid)
        if not L:
            return await e.answer("این بازی تموم شده", alert=True)
        if e.sender_id != L["creator"]:
            return await e.answer("فقط سازنده می‌تونه با ربات بازی کنه", alert=True)
        async with L["lock"]:
            duel_ok = L["type"] == "duel" and L["kind"] == 2 and len(L["players"]) == 1
            table_ok = L["type"] == "casino" and len(L["players"]) < L["n"]
            if L["state"] != "open" or not (duel_ok or table_ok):
                return await e.answer("الان نمیشه", alert=True)
            L["state"] = "running"
            if duel_ok:
                L["players"].append((0, "🤖 ربات"))
            else:
                k = 1
                while len(L["players"]) < L["n"]:
                    L["players"].append((0, f"🤖 ربات {k}"))
                    k += 1
        L["task"].cancel()
        await e.answer("🤖")
        await (run_duel(lid) if L["type"] == "duel" else run_table(lid))

    @bot.on(events.CallbackQuery(pattern=rb"^lc:"))
    async def cb_cancel(e):
        lid = int(e.data.decode().split(":")[1])
        L = lobbies.get(lid)
        if not L:
            return await e.answer("این بازی تموم شده", alert=True)
        if e.sender_id != L["creator"] and e.sender_id not in admin_ids:
            return await e.answer("فقط سازنده می‌تونه لغو کنه", alert=True)
        L["task"].cancel()
        await cancel_lobby(lid, "❌ لغو شد؛ شرط‌ها برگشت داده شد.")
        await e.answer("لغو شد")

    @bot.on(events.CallbackQuery(pattern=rb"^gi:"))
    async def cb_info(e):
        """Info buttons of the balance card."""
        bal = await db.balance(e.sender_id)
        await e.answer(f"💎 موجودی شما: {db.fmt(bal)}\n💰 ارزش: {db.value_text(bal)}", alert=True)
