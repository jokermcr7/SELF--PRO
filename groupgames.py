"""Diamond games (all commands in Persian), played ONLY in the special game group. Runs on the manager bot.

Games (+ رولت، چالش), each opens a button panel (banner + coloured buttons):
  بازی 20  دونفره یا سه‌نفره (کاملاً شانسی؛ عدد بزرگ‌تر همه‌ی الماس‌ها رو می‌بره) — دونفره با ربات هم میشه (BOT_WIN_PERCENT، پیش‌فرض ۵۰ برد / ۵۰ باخت)
  تاس      زوج / فرد (×1.5)  یا  حدس دقیق (×2)  — against the bot
  کازینو   تعیین مبلغ ورودی + تک‌نفره (اسلات با ربات) یا دونفره (دو بازیکن با هم)
  سکه      شیر یا خط — با ربات (برد ×2) یا با یه کاربر دیگه

Commands (game group):  بازی [مبلغ] | تاس | سکه | کازینو | بازی ها | بازی تاس / بازی سکه / بازی کازینو | راهنما
Everywhere:             موجودی  |  انتقال [مبلغ] (ریپلای)  |  برترین
Admin:                  تنظیم بازی

FAIR PLAY (v1.7): coin / «بازی 20» outcomes come from random.org (falls back to the OS crypto generator). Dice and slot use the
REAL animated Telegram dice (uniform server randomness) so everyone can watch the spin. Every player / the bot has exactly the same win chance:
  تاس زوج/فرد 50% ×2  |  حدس دقیق 1/6 ×6  |  سکه با ربات 50% ×2  |  کازینو تک‌نفره 50% ×2  |  بازی 20 با ربات 50%
  بازی دو/سه‌نفره، دو نفره‌ی سکه و کازینو: فقط بین بازیکن‌هاست و FEE درصد مالیات می‌گیره؛ «بازی 20» با ربات: ۵۰٪ برد / ۵۰٪ باخت (BOT_WIN_PERCENT).
"""
import asyncio
import html
import itertools
import logging
import os
import random
import re
import time

import aiohttp
from telethon import events, types

import botapi
import db
import gameplus
import utils
from botapi import GREEN, RED, BLUE, btn, kb

log = logging.getLogger("games")

MIN_BET = 20                                               # minimum entry for EVERY game
HARD_CAP = 10 ** 9                                         # "unlimited" (only keeps numbers inside BIGINT range)
MAX_BET = int(os.getenv("MAX_GAME_BET", "0")) or HARD_CAP   # 0 / unset = no limit
FEE = int(os.getenv("GAME_FEE_PERCENT", "5"))              # PvP tax: pot 88 -> tax 4 -> prize 84
BRAND = os.getenv("BRAND", "Vip MMD")
_rng = random.SystemRandom()          # OS cryptographic randomness (os.urandom)
BOT_WIN_PERCENT = min(max(float(os.getenv("BOT_WIN_PERCENT", "50")), 0.0), 100.0)   # «بازی 20» with the bot: player win chance
LOBBY_TIMEOUT = 180
PANEL_TIMEOUT = 240
MAX_OPEN_PER_USER = 2

# ---- payout table (total return = bet x multiplier; the stake is already taken)
DICE_PARITY_MULT = 2.0          # even / odd           P=1/2  -> fair (RTP 100%)
DICE_EXACT_MULT = 6.0           # exact number         P=1/6  -> fair (RTP 100%)
COIN_WIN_P = float(os.getenv("COIN_WIN_P", "0.5"))   # vs bot, win = x2 -> RTP 98%
CASINO_MULT = 2.0               # casino solo: win chance 50% -> fair
SLOT_777, SLOT_FRUIT, SLOT_PAIR = 3.0, 2.0, 1.3   # (legacy table, only used to rank reels)

lobbies: dict[int, dict] = {}            # open PvP games (id = db lobby id)
panels: dict[int, dict] = {}             # setup panels (id = counter)
awaiting: dict[tuple, tuple] = {}        # (chat, user) -> (panel id, deadline)   custom amount typing
_cool: dict[int, float] = {}
_ids = itertools.count(int(time.time()) % 10 ** 8)

SYM = ["BAR", "🍇", "🍋", "7️⃣"]
RL_RED = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
RL_MODES = {"red": "🔴 قرمز", "black": "⚫ سیاه", "even": "زوج", "odd": "فرد", "low": "۱ تا ۱۸", "high": "۱۹ تا ۳۶", "exact": "🎯 عدد دقیق"}
RL_EXACT_MULT = 36.0                                  # 1/36 -> fair
RL_SIDE_MULT = 2.0                                    # 18/36 -> fair
GAME_TITLE = {"duel": "بازی دو/سه‌نفره 🎲", "casino": "کازینو 🎰", "dice": "تاس 🎲", "coin": "شیر یا خط 🪙", "roulette": "رولت 🎡"}
GAME_WORDS = {"تاس": "dice", "سکه": "coin", "کازینو": "casino", "رولت": "roulette"}


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


def pay_of(bet, mult):
    """Total return for a win. Rounded DOWN so the house never pays a fraction of a diamond extra."""
    return int(bet * mult)


RANDOM_ORG = "https://www.random.org/integers/"
_rorg_down_until = 0.0


async def rnd_ints(n, lo, hi):
    """n independent uniform integers lo..hi from random.org (true randomness). If random.org is unreachable /
    rate limited we fall back to the OS cryptographic generator for a minute, so a game never hangs."""
    global _rorg_down_until
    if time.time() >= _rorg_down_until:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as ses:
                async with ses.get(RANDOM_ORG, params={"num": n, "min": lo, "max": hi, "col": 1, "base": 10,
                                                       "format": "plain", "rnd": "new"}) as r:
                    txt = (await r.text()).strip()
                    if r.status == 200:
                        vals = [int(x) for x in txt.split()]
                        if len(vals) == n and all(lo <= v <= hi for v in vals):
                            return vals
                    raise ValueError(f"random.org {r.status}: {txt[:60]}")
        except Exception as ex:                                    # noqa: BLE001
            _rorg_down_until = time.time() + 60
            log.warning("random.org unavailable (%s) - using os.urandom for 60s", ex)
    return rand_ints(n, lo, hi)


async def rnd_bool():
    """Fair coin: True / False with exactly 50% each."""
    return bool((await rnd_ints(1, 0, 1))[0])


DICE_FACE = "⚀⚁⚂⚃⚄⚅"


def reels_for(win):
    """Display reels for the casino: a winner gets a matching row (777 / fruit), a loser three different symbols."""
    if win:
        k = _rng.choice([3, 3, 1, 2])
        return [k, k, k]
    return _rng.sample(range(4), 3)


def rand_ints(n, lo, hi):
    """n independent uniformly random integers lo..hi from the OS cryptographic generator (random.SystemRandom)."""
    return [_rng.randint(lo, hi) for _ in range(n)]


def bot_duel_numbers(human_wins):
    """Display numbers for «بازی 20» vs the bot. The outcome is decided first (human wins with BOT_WIN_PERCENT %),
    then two different numbers 1..1000 are drawn at random and ordered to match it. Returns (human, bot)."""
    hi = _rng.randint(2, 1000)
    lo = _rng.randint(1, hi - 1)
    return (hi, lo) if human_wins else (lo, hi)


def slot_eval(v):
    """Telegram slot value 1..64 -> (reels, multiplier, label, rank).
    64 = 777, 1/22/43 = three of a kind (BAR BAR BAR = 1). rank orders results in 2-player games."""
    r = [((v - 1) >> (2 * i)) & 3 for i in range(3)]
    if r[0] == r[1] == r[2]:
        if r[0] == 3:
            return r, SLOT_777, "🎉 جکپات ۷۷۷", SLOT_777
        if r[0] == 0:
            return r, 0.0, "💀 سه‌تا BAR — باخت کامل", -1.0
        return r, SLOT_FRUIT, "✨ سه‌تا مثل هم", SLOT_FRUIT
    if len(set(r)) == 2:
        return r, SLOT_PAIR, "🍒 دوتا مثل هم", SLOT_PAIR
    return r, 0.0, "😢 نشد", 0.0


def reels_txt(r):
    return " ".join(SYM[i] for i in r)


async def recover(bot):
    """After a crash/restart: refund every bet that was locked in an open lobby."""
    for r in await db.lobby_all():
        for uid in r["players"]:
            await db.add(uid, r["bet"], "game_refund_restart")
        await db.lobby_delete(r["id"])


LN = "┄┄┄┄┄┄┄┄┄┄┄┄┄┄"
HELP = (f"<b><i>• — {html.escape(BRAND)} — •</i></b>\n\n<b>🎮 راهنمای بازی‌ها</b>\n<i>(فقط توی گپ مخصوص بازی)</i>\n{LN}\n"
        "🎲 <code>بازی 20</code> — دو/سه‌نفره یا با ربات 🤖؛ عدد بزرگ‌تر کل جایزه رو می‌بره\n"
        "🎲 <code>تاس</code> — زوج/فرد <b>×2</b> یا حدس دقیق عدد <b>×6</b>\n"
        "🎰 <code>کازینو</code> — تک‌نفره (تو و ربات) یا دونفره؛ نتیجه‌ی بهتر می‌بره <b>×2</b>\n"
        "🪙 <code>سکه</code> — شیر یا خط با ربات <b>×2</b> یا با یه کاربر\n"
        "🎡 <code>رولت</code> — رنگ/زوج‌وفرد/بازه <b>×2</b> یا عدد دقیق ۱ تا ۳۶ <b>×36</b>\n"
        "🙈 <code>چالش 50</code> — دو نفر عدد مخفی می‌زنن؛ random.org عدد هدف رو می‌ده\n"
        f"{LN}\n"
        "🍀 همه‌ی بازی‌ها شانسیه و شانس برد همه (و ربات) برابره؛ قرعه‌ی سکه و بازی ۲۰ از random.org\n"
        f"{LN}\n"
        "📋 <code>بازی ها</code> — منوی بازی‌ها\n"
        "💎 <code>موجودی</code> | 🏆 <code>برترین</code> | 🔁 <code>انتقال 25</code> (ریپلای)\n"
        "✏️ مبلغ رو بعد از اسم بازی بنویس: <code>بازی 50</code> | <code>تاس 50</code> | <code>بازی کازینو 50</code>")


def register(bot, admin_ids):
    async def game_chat():
        return int(await db.get("game_group", os.getenv("GAME_GROUP_ID", "0")) or 0)

    plus_handle = gameplus.register(bot, admin_ids, {
        "rnd_ints": rnd_ints, "rnd_bool": rnd_bool, "FEE": FEE, "MIN_BET": MIN_BET, "MAX_BET": MAX_BET,
        "name_of": name_of, "game_chat": game_chat})

    # ================================================================ shared helpers
    def clamp(bet):
        return max(MIN_BET, min(MAX_BET, bet))

    def open_count(uid):
        return sum(1 for L in lobbies.values() if L["creator"] == uid)

    def bet_head(P):
        return (f"<b><i>• — {html.escape(BRAND)} — •</i></b>\n\n<b>• {GAME_TITLE[P['game']]}</b>\n{LN}\n"
                f"👤 بازیکن: {esc(P['name'])}\n💎 مبلغ ورودی: <b>{P['bet']:,} الماس</b>\n{LN}\n")

    def pot_of(L):
        return L["bet"] * L["kind"]

    def tax_of(L):
        return pot_of(L) * FEE // 100

    def prize_of(L):
        return pot_of(L) - tax_of(L)

    def prize_pvp(bet):
        pot = bet * 2
        return pot - pot * FEE // 100

    def tax_pvp(bet):
        return bet * 2 * FEE // 100

    def ck(cond):
        return "✅ " if cond else ""

    # ================================================================ panel rendering
    def panel_view(P):
        g, v, bet, pid = P["game"], P["view"], P["bet"], P["id"]
        if v == "bet":
            t = (bet_head(P) + f"\n💎 <b>مبلغ ورودی رو انتخاب کن</b> (مثلاً 20 الماس)\nحداقل {MIN_BET:,} الماس — {limit_txt(MAX_BET)}")
            presets = [x for x in (20, 50, 100, 500, 1000, 5000) if MIN_BET <= x <= MAX_BET][:6]
            rows = [[btn(f"{x:,} 💎", f"gp:{pid}:s:{x}", GREEN) for x in presets[i:i + 3]] for i in range(0, len(presets), 3)]
            rows.append([btn("✏️ مبلغ دلخواه", f"gp:{pid}:bc", BLUE), btn("↩️ برگشت", f"gp:{pid}:m", RED)])
            return t, kb(rows)
        if v == "custom":
            return (bet_head(P) + f"\n✏️ <b>مبلغ دلخواهت رو همین‌جا توی گروه بفرست</b>\nحداقل {MIN_BET:,} الماس — {limit_txt(MAX_BET)} (تا ۶۰ ثانیه)",
                    kb([[btn("↩️ برگشت", f"gp:{pid}:m", RED)]]))
        bet_btn = btn(f"💎 تعیین مبلغ ورودی · {bet:,} الماس", f"gp:{pid}:b", BLUE)
        cancel = btn("❌ انصراف", f"gp:{pid}:x", RED)

        if g == "duel":
            t = (bet_head(P) + "\n<b>نوع بازی رو انتخاب کن 👇</b>\n"
                 f"👥 دونفره: جایزه {bet * 2 - bet * 2 * FEE // 100:,} 💎 (می‌تونی با ربات هم بازی کنی 🤖)\n"
                 f"👥 سه‌نفره: جایزه {bet * 3 - bet * 3 * FEE // 100:,} 💎\n"
                 f"🍀 کاملاً شانسی: به هر نفر یه عدد میاد؛ عدد بزرگ‌تر همه‌ی الماس‌ها رو می‌بره\n"
                 f"➖ مالیات: {FEE}٪")
            return t, kb([[btn("👥 دو‌نفره", f"gp:{pid}:d2", GREEN), btn("👥👥 سه‌نفره", f"gp:{pid}:d3", GREEN)],
                          [bet_btn], [cancel]])

        if g == "dice":
            mode = P["mode"]
            t = (bet_head(P) + "\n<b>یکی از حالت‌ها رو انتخاب کن 👇</b>\n"
                 f"{ck(mode == 'even')}زوج (۲ ، ۴ ، ۶) ← <b>×{DICE_PARITY_MULT:g}</b> ({pay_of(bet, DICE_PARITY_MULT):,} 💎)\n"
                 f"{ck(mode == 'odd')}فرد (۱ ، ۳ ، ۵) ← <b>×{DICE_PARITY_MULT:g}</b> ({pay_of(bet, DICE_PARITY_MULT):,} 💎)\n"
                 f"{ck(mode == 'exact')}🎯 حدس دقیق عدد ← <b>×{DICE_EXACT_MULT:g}</b> ({pay_of(bet, DICE_EXACT_MULT):,} 💎)")
            if mode == "exact":
                t += f"\n\n🎯 عددی که حدس می‌زنی: <b>{P['pick'] or '— انتخاب کن'}</b>"
            rows = [[btn(ck(mode == "even") + "زوج", f"gp:{pid}:md:even", GREEN),
                     btn(ck(mode == "odd") + "فرد", f"gp:{pid}:md:odd", GREEN),
                     btn(ck(mode == "exact") + "🎯 حدس دقیق", f"gp:{pid}:md:exact", GREEN)]]
            if mode == "exact":
                rows.append([btn(ck(P["pick"] == i) + str(i), f"gp:{pid}:p:{i}", BLUE) for i in range(1, 7)])
            rows += [[bet_btn], [btn("🎲 بنداز!", f"gp:{pid}:go", GREEN)], [cancel]]
            return t, kb(rows)

        if g == "coin":
            vs, side = P["vs"], ("شیر 🦁" if P["pick"] == "h" else "خط ✍️")
            t = (bet_head(P) + f"\n🆚 حریف: <b>{'ربات 🤖' if vs == 'bot' else 'یه کاربر دیگه 👤'}</b>\n🎯 انتخاب تو: <b>{side}</b>\n")
            if vs == "bot":
                t += f"🏆 اگه ببری: <b>×2</b> ({bet * 2:,} 💎)"
            else:
                t += (f"🏆 جایزه‌ی برنده: <b>{prize_pvp(bet):,} 💎</b> (مالیات {FEE}٪)\n"
                      "حریفی که بپیونده خودکار طرف مقابل رو می‌گیره.")
            rows = [[btn(ck(vs == "bot") + "🤖 با ربات", f"gp:{pid}:vs:bot", GREEN), btn(ck(vs == "user") + "👤 با کاربر", f"gp:{pid}:vs:user", GREEN)],
                    [btn(ck(P["pick"] == "h") + "شیر 🦁", f"gp:{pid}:p:h", BLUE), btn(ck(P["pick"] == "t") + "خط ✍️", f"gp:{pid}:p:t", BLUE)],
                    [bet_btn],
                    [btn("🪙 بنداز!" if vs == "bot" else "👥 ساخت بازی", f"gp:{pid}:go", GREEN)], [cancel]]
            return t, kb(rows)

        if g == "roulette":
            mode = P["mode"]
            t = (bet_head(P) + "\n<b>روی چی شرط می‌بندی؟ 👇</b>\n"
                 f"🔴 قرمز / ⚫ سیاه / زوج / فرد / ۱-۱۸ / ۱۹-۳۶ ← <b>×{RL_SIDE_MULT:g}</b> ({pay_of(bet, RL_SIDE_MULT):,} 💎)\n"
                 f"🎯 عدد دقیق (۱ تا ۳۶) ← <b>×{RL_EXACT_MULT:g}</b> ({pay_of(bet, RL_EXACT_MULT):,} 💎)\n"
                 "🍀 اعداد ۱ تا ۳۶ (بدون صفر)؛ شانس همه برابره و عدد از random.org میاد\n"
                 f"✅ انتخاب تو: <b>{RL_MODES[mode]}{(' ' + str(P['pick'])) if mode == 'exact' and P['pick'] else ''}</b>")
            def mb(k):
                return btn(ck(mode == k) + RL_MODES[k], f"gp:{pid}:md:{k}", GREEN if k != "exact" else BLUE)
            rows = [[mb("red"), mb("black")], [mb("even"), mb("odd")], [mb("low"), mb("high")], [mb("exact")]]
            if mode == "exact":
                rows += [[btn(ck(P["pick"] == i) + str(i), f"gp:{pid}:p:{i}", BLUE) for i in range(r, r + 6)] for r in range(1, 37, 6)]
            rows += [[bet_btn], [btn("🎡 بچرخون!", f"gp:{pid}:go", GREEN)], [cancel]]
            return t, kb(rows)

        # casino
        solo = P["n"] == 1
        rules = "(۷۷۷ > سه‌تا مثل هم > دوتا مثل هم > هیچی > BAR BAR BAR). مساوی شد دوباره می‌چرخن."
        if solo:
            t = (bet_head(P) + "👤 حالت: <b>تک‌نفره</b> (تو و ربات)\n\nاول تو دستگاه رو می‌چرخونی، بعد ربات؛ "
                 f"نتیجه‌ی بهتر برنده‌ست {rules}\n"
                 "شانس برد تو و ربات دقیقاً <b>برابره</b> 🍀\n"
                 f"🏆 اگه ببری: <b>×{CASINO_MULT:g}</b> ({pay_of(bet, CASINO_MULT):,} 💎) | باخت: ورودی می‌سوزه")
        else:
            t = (bet_head(P) + "👥 حالت: <b>دونفره</b>\n\nهر دو نفر یه بار دستگاه رو می‌چرخونن؛ نتیجه‌ی بهتر برنده‌ست "
                 f"{rules}\n🏆 جایزه‌ی برنده: <b>{prize_pvp(bet):,} 💎</b> (مالیات {FEE}٪)")
        rows = [[bet_btn],
                [btn(ck(solo) + "👤 تک‌نفره", f"gp:{pid}:sn:1", GREEN), btn(ck(not solo) + "👥 دونفره", f"gp:{pid}:sn:2", GREEN)],
                [btn("🎰 بچرخون!" if solo else "🎰 ساخت میز دونفره", f"gp:{pid}:go", GREEN)], [cancel]]
        return t, kb(rows)

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
        P = {"id": next(_ids), "chat": chat, "msg": 0, "owner": uid, "name": name, "game": game, "bet": clamp(bet or MIN_BET),
             "n": 2 if n == 2 else 1, "mode": "even", "vs": "bot", "pick": "h" if game == "coin" else None,
             "view": "main", "state": "setup", "lock": asyncio.Lock()}
        if game == "roulette":
            P["mode"] = "red"
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

    # ================================================================ PvP lobbies (coin vs user, casino 2 players)
    def lobby_text(L, note=None):
        creator = L["players"][0][1]
        head = f"<b><i>• — {html.escape(BRAND)} — •</i></b>\n\n"
        if L["type"] == "duel":
            names = "، ".join(esc(n) for _, n in L["players"])
            t = (head + f"<b>• بازی جدید 🎲</b>\n👤 سازنده:\n{esc(creator)}\n\n👥 تعداد بازیکنان:\n{L['kind']} 🇮🇷\n\n"
                 f"💎 مقدار:\n{L['bet']:,} 💎\n\n🏆 جایزه برنده:\n{prize_of(L):,} 💎\n\n➖ مالیات:\n{tax_of(L):,} 💎\n")
            if len(L["players"]) > 1:
                t += f"\n🙋 پیوسته‌ها: {names}\n"
            return t + (f"\nدر انتظار بازیکن {len(L['players']) + 1} از {L['kind']} •••" if note is None else note)
        if L["type"] == "coin":
            mine = "شیر 🦁" if L["side"] == "h" else "خط ✍️"
            other = "خط ✍️" if L["side"] == "h" else "شیر 🦁"
            t = (head + f"<b>• شیر یا خط 🪙</b>\n👤 سازنده: {esc(creator)} ← <b>{mine}</b>\n"
                 f"🆚 حریف: <b>{other}</b> رو می‌گیره\n")
        else:
            t = (head + f"<b>• کازینو دونفره 🎰</b>\n👤 سازنده: {esc(creator)}\n"
                 "هر دو یه بار می‌چرخونن؛ نتیجه‌ی بهتر می‌بره.\n")
        t += (f"💎 مبلغ ورودی: <b>{L['bet']:,}</b>\n🏆 جایزه‌ی برنده: <b>{prize_pvp(L['bet']):,}</b> 💎 | "
              f"➖ مالیات: {tax_pvp(L['bet']):,}\n")
        return t + ("\nدر انتظار حریف •••" if note is None else note)

    def lobby_markup(lid, L):
        if L["type"] == "duel":
            rows = [[btn("پیوستن ✅", f"lj:{lid}", GREEN), btn("لغو بازی ❌", f"lc:{lid}", RED)]]
            if L["kind"] == 2:
                rows.append([btn("بازی با ربات 🤖", f"lb:{lid}", BLUE)])
            return kb(rows)
        label = f"✅ پیوستن ({L['bet']:,} 💎)"
        if L["type"] == "coin":
            label += " ← " + ("خط ✍️" if L["side"] == "h" else "شیر 🦁")
        return kb([[btn(label, f"lj:{lid}", GREEN), btn("لغو ❌", f"lc:{lid}", RED)]])

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

    async def new_lobby(P, kind=2):
        """Panel -> PvP lobby on the same message. The creator pays the entry now. Returns (error_text|None, lobby_id)."""
        uid, ltype = P["owner"], P["game"]
        if open_count(uid) >= MAX_OPEN_PER_USER:
            return "تو همین الان چند بازی باز داری؛ اول اونا تموم بشن.", None
        if await db.try_spend(uid, P["bet"], "game_bet" if ltype == "duel" else f"{ltype}_bet") is None:
            return "💎 موجودی کافی نیست", None
        lid = await db.lobby_create(P["chat"], P["bet"], kind if ltype == "duel" else (200 if ltype == "coin" else 100) + 2, uid)
        L = {"type": ltype, "chat": P["chat"], "msg": P["msg"], "bet": P["bet"], "kind": kind, "creator": uid,
             "players": [(uid, P["name"])], "lock": asyncio.Lock(), "state": "open", "side": P["pick"]}
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
        """Pure luck. PvP (2-3 humans): every player draws a random number 1..1000, highest takes the pot, ties redraw.
        Vs the bot: the player wins with BOT_WIN_PERCENT % (default 50 / 50), numbers are shown for the look."""
        L = lobbies[lid]
        try:
            players = L["players"]
            nums = [[] for _ in players]
            bot_game = any(u == 0 for u, _ in players)
            if bot_game:
                h = next(k for k, (u, _) in enumerate(players) if u != 0)
                human_wins = (await rnd_ints(1, 1, 10000))[0] <= int(round(BOT_WIN_PERCENT * 100))
                await edit_lobby(L, lobby_text(L, "") + "\n\n🎲 قرعه‌کشی ...", kb([]))
                await asyncio.sleep(1.5)
                hv, bv = bot_duel_numbers(human_wins)
                for k in range(len(players)):
                    nums[k].append(hv if k == h else bv)
                win = h if human_wins else 1 - h
            else:
                order = list(range(len(players)))
                for _rnd in range(4):
                    live = lobby_text(L, "")
                    lines = "\n".join(f"🍀 {esc(players[k][1])}: " + (" ← ".join(map(str, nums[k])) or "—") for k in range(len(players)))
                    await edit_lobby(L, f"{live}\n\n{lines}\n\n🎲 قرعه‌کشی ...", kb([]))
                    vals = await rnd_ints(len(order), 1, 1000)
                    await asyncio.sleep(1.5)
                    drawn = {}
                    for i, v in zip(order, vals):
                        nums[i].append(v)
                        drawn[i] = v
                    top = max(drawn.values())
                    order = [i for i, v in drawn.items() if v == top]
                    if len(order) == 1:
                        break
                win = order[0] if len(order) == 1 else order[(await rnd_ints(1, 0, len(order) - 1))[0]]
            wid, wname = players[win]
            prize = prize_of(L)
            if wid:
                await db.add(wid, prize, "game_win")
            lines = "\n".join(f"{'🏆' if k == win else '▫️'} {esc(players[k][1])}: " + " ← ".join(map(str, nums[k]))
                              for k in range(len(players)))
            text = (f"<b><i>• — {html.escape(BRAND)} — •</i></b>\n\n<b>🍀 شرط تمام شد</b>\n\n{lines}\n\n"
                    f"🏆 برنده: <b>{esc(wname)}</b>\n💎 جایزه: <b>{prize:,}</b> | ➖ مالیات: {tax_of(L):,}"
                    )
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

    async def run_coin_pvp(lid):
        """Fair 50/50 flip between two players; the winner gets both entries minus the tax."""
        L = lobbies[lid]
        try:
            (cid, cname), (jid, jname) = L["players"]
            await edit_lobby(L, lobby_text(L, "\n🪙 سکه در حال چرخشه ..."), kb([]))
            await asyncio.sleep(2.5)
            res = "h" if await rnd_bool() else "t"
            wid, wname = (cid, cname) if res == L["side"] else (jid, jname)
            prize = prize_pvp(L["bet"])
            await db.add(wid, prize, "coin_win")
            nm = lambda x: "شیر 🦁" if x == "h" else "خط ✍️"
            text = (f"<b><i>• — {html.escape(BRAND)} — •</i></b>\n\n<b>🪙 نتیجه‌ی شیر یا خط</b>\n\n"
                    f"▫️ {esc(cname)}: {nm(L['side'])}\n▫️ {esc(jname)}: {nm('t' if L['side'] == 'h' else 'h')}\n\n"
                    f"🪙 آمد: <b>{nm(res)}</b>\n🏆 برنده: <b>{esc(wname)}</b>\n"
                    f"💎 جایزه: <b>{prize:,}</b> | ➖ مالیات: {tax_pvp(L['bet']):,}")
            await edit_lobby(L, text, kb([[btn(f"🔁 بازی جدید ({L['bet']:,} 💎)", f"gr:coin:{L['bet']}:2", GREEN)]]))
        except Exception:
            log.exception("coin pvp failed, refunding")
            for u, _ in L["players"]:
                if u:
                    await db.add(u, L["bet"], "game_refund_error")
            await edit_lobby(L, "⚠️ خطا توی بازی؛ شرط‌ها برگشت داده شد.", kb([]))
        finally:
            lobbies.pop(lid, None)
            await db.lobby_delete(lid)

    async def run_casino_pvp(lid):
        """Both players spin the slot; the better result wins both entries minus the tax. Ties re-spin (3 times), then refund."""
        L = lobbies[lid]
        try:
            players = L["players"]
            res, winner = [], None
            for _rnd in range(4):
                res = []
                for uid_, nm in players:
                    await edit_lobby(L, lobby_text(L, f"\n⏳ نوبت <b>{esc(nm)}</b> ..."), kb([]))
                    v = await roll(L["chat"], "🎰", L["msg"], 4.4)
                    res.append((nm,) + slot_eval(v))
                if res[0][4] != res[1][4]:
                    winner = 0 if res[0][4] > res[1][4] else 1
                    break
            lines = "\n".join(f"{'🏆' if winner == i else '▫️'} {esc(nm)}: {reels_txt(r)} — {label}"
                              for i, (nm, r, _m, label, _rk) in enumerate(res))
            head = f"<b><i>• — {html.escape(BRAND)} — •</i></b>\n\n<b>🎰 نتیجه‌ی کازینو دونفره</b>\n💎 ورودی: {L['bet']:,}\n\n{lines}\n\n"
            if winner is None:
                for u, _ in players:
                    if u:
                        await db.add(u, L["bet"], "game_refund")
                text = head + "🤝 چند بار مساوی شد؛ ورودی هر دو نفر برگشت داده شد."
            else:
                prize = prize_pvp(L["bet"])
                await db.add(players[winner][0], prize, "casino_win")
                text = head + f"🏆 برنده: <b>{esc(players[winner][1])}</b>\n💎 جایزه: <b>{prize:,}</b> | ➖ مالیات: {tax_pvp(L['bet']):,}"
            await edit_lobby(L, text, kb([[btn(f"🔁 میز جدید ({L['bet']:,} 💎)", f"gr:casino:{L['bet']}:2", GREEN)]]))
        except Exception:
            log.exception("casino pvp failed, refunding")
            for u, _ in L["players"]:
                if u:
                    await db.add(u, L["bet"], "game_refund_error")
            await edit_lobby(L, "⚠️ خطا توی بازی؛ شرط‌ها برگشت داده شد.", kb([]))
        finally:
            lobbies.pop(lid, None)
            await db.lobby_delete(lid)

    # ================================================================ solo games (against the house)
    async def finish(P, text):
        P["state"] = "done"
        P["view"] = "main"
        touch(P)
        try:
            await botapi.edit_banner(P["chat"], P["msg"], text, again_markup(P))
        except Exception:
            log.exception("result edit failed")

    async def play_solo(P):
        g, bet, uid = P["game"], P["bet"], P["owner"]
        head = f"<b><i>• — {html.escape(BRAND)} — •</i></b>\n\n<b>• {GAME_TITLE[g]}</b>\n👤 {esc(P['name'])} | 💎 ورودی: {bet:,}\n\n"
        if g == "dice" and P["mode"] == "exact" and not P["pick"]:
            return "🎯 اول عددی که حدس می‌زنی رو انتخاب کن"
        if g == "roulette" and P["mode"] == "exact" and not P["pick"]:
            return "🎯 اول عددی که حدس می‌زنی (۱ تا ۳۶) رو انتخاب کن"
        if await db.try_spend(uid, bet, f"{g}_bet") is None:
            return "💎 موجودی کافی نیست"
        _won = False
        P["state"] = "running"
        try:
            await botapi.edit_banner(P["chat"], P["msg"], head + "⏳ در حال بازی ...", kb([]))
            if g == "dice":
                v = await roll(P["chat"], "🎲", P["msg"], 3.6)
                mode = P["mode"]
                if mode == "exact":
                    win, mult, mine = v == P["pick"], DICE_EXACT_MULT, f"حدس دقیق: {P['pick']}"
                else:
                    win, mult = (v % 2 == 0) == (mode == "even"), DICE_PARITY_MULT
                    mine = "زوج" if mode == "even" else "فرد"
                pay = pay_of(bet, mult) if win else 0
                _won = win
                bal = await db.add(uid, pay, "dice_win") if pay else await db.balance(uid)
                line = f"🏆 بردی! <b>+{pay - bet:,}</b> 💎" if win else f"💥 باختی! <b>-{bet:,}</b> 💎"
                body = f"🎯 انتخاب تو: {mine}\n🎲 آمد: <b>{v}</b> ({'زوج' if v % 2 == 0 else 'فرد'})\n{line}"
            elif g == "coin":
                res = P["pick"] if (await rnd_ints(1, 1, 10000))[0] <= int(round(COIN_WIN_P * 10000)) else ("t" if P["pick"] == "h" else "h")
                nm = lambda x: "شیر 🦁" if x == "h" else "خط ✍️"
                _won = res == P["pick"]
                if res == P["pick"]:
                    bal, line = await db.add(uid, bet * 2, "coin_win"), f"🏆 بردی! <b>+{bet:,}</b> 💎"
                else:
                    bal, line = await db.balance(uid), f"💥 باختی! <b>-{bet:,}</b> 💎"
                body = f"🪙 انتخاب تو: {nm(P['pick'])}\n🪙 آمد: <b>{nm(res)}</b>\n{line}"
            elif g == "roulette":
                v = (await rnd_ints(1, 1, 36))[0]
                await asyncio.sleep(1.5)
                mode = P["mode"]
                color = "🔴" if v in RL_RED else "⚫"
                win = {"red": v in RL_RED, "black": v not in RL_RED, "even": v % 2 == 0, "odd": v % 2 == 1,
                       "low": v <= 18, "high": v >= 19, "exact": v == P["pick"]}[mode]
                mult = RL_EXACT_MULT if mode == "exact" else RL_SIDE_MULT
                pay = pay_of(bet, mult) if win else 0
                _won = win
                bal = await db.add(uid, pay, "roulette_win") if pay else await db.balance(uid)
                line = f"🏆 بردی! <b>+{pay - bet:,}</b> 💎" if win else f"💥 باختی! <b>-{bet:,}</b> 💎"
                mine = RL_MODES[mode] + (f" {P['pick']}" if mode == "exact" else "")
                body = f"🎯 انتخاب تو: {mine}\n🎡 آمد: <b>{v}</b> {color} ({'زوج' if v % 2 == 0 else 'فرد'})\n{line}"
            else:  # casino, solo
                res_txt, outcome = "", None
                for _rnd in range(4):                     # tie -> both spin again (max 4 rounds), then refund
                    await botapi.edit_banner(P["chat"], P["msg"], head + "🎰 نوبت <b>تو</b> ...", kb([]))
                    mv = await roll(P["chat"], "🎰", P["msg"], 4.4)
                    await botapi.edit_banner(P["chat"], P["msg"], head + "🎰 نوبت <b>ربات</b> ...", kb([]))
                    bv = await roll(P["chat"], "🎰", P["msg"], 4.4)
                    mr, _m1, mlabel, mrank = slot_eval(mv)
                    br, _m2, blabel, brank = slot_eval(bv)
                    res_txt = f"👤 تو: {reels_txt(mr)} — {mlabel}\n🤖 ربات: {reels_txt(br)} — {blabel}\n"
                    if mrank != brank:
                        outcome = mrank > brank
                        break
                reels = None
                _won = outcome is True
                if outcome is True:
                    pay, label = pay_of(bet, CASINO_MULT), "🏆 بردی!"
                elif outcome is False:
                    pay, label = 0, "😢 باختی"
                else:
                    pay, label = bet, "🤝 چند بار مساوی شد؛ ورودی برگشت"
                bal = await db.add(uid, pay, "casino_win") if pay else await db.balance(uid)
                net = pay - bet
                body = f"{res_txt}{label}\n" + (f"🏆 <b>{net:+,}</b> 💎" if net > 0 else (
                    "🤝 شرطت برگشت" if net == 0 else f"💥 <b>{net:,}</b> 💎"))
            await finish(P, head + body + f"\n\n💎 موجودی: <b>{db.fmt(bal)}</b>")
        except Exception:
            log.exception("solo game failed, refunding")
            await db.add(uid, bet, f"{g}_refund")
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
    CMD = {"موجودی": "bal", "برترین": "top", "برترینها": "top", "انتقال": "give", "بازیها": "menu",
           "کازینو": "casino", "تاس": "dice", "سکه": "coin", "راهنما": "help", "راهنمای": "help",
           "رولت": "roulette", "چالش": "chal"}

    def parse(t):
        w = t.split()
        if not w:
            return None, ""
        if w[0].startswith("/"):                       # only the old admin command is kept (hidden alias)
            return ("setgame", "") if re.match(r"^/setgame(@\w+)?$", w[0]) else (None, "")
        n0, n1 = nf(w[0]), nf(w[1]) if len(w) > 1 else ""
        if n0 == "تنظیم" and n1 == "بازی":
            return "setgame", ""
        if n0 == "بازی":
            if n1 == "ها":
                return "menu", ""
            if n1 in GAME_WORDS:                       # «بازی تاس 50»
                w, n0 = w[1:], n1
            elif len(w) == 1 or w[1].isdigit():        # «بازی» | «بازی 20» | «بازی 20 3»
                return "duel", " ".join(w[1:])
            else:
                return None, ""
        cmd = CMD.get(n0)
        if cmd in ("casino", "dice", "coin", "roulette", "chal") and not (len(w) == 1 or (len(w) == 2 and w[1].isdigit())):
            return None, ""                           # «سکه خوبیه» is just chat, not a command
        return cmd, " ".join(w[1:])

    def menu_markup():
        return kb([[btn("🎲 بازی دو/سه‌نفره", "gm:duel", GREEN)],
                   [btn("🎲 تاس", "gm:dice", GREEN), btn("🪙 شیر یا خط", "gm:coin", GREEN)],
                   [btn("🎰 کازینو", "gm:casino", GREEN), btn("🎡 رولت", "gm:roulette", GREEN)],
                   [btn("🙈 چالش عدد مخفی", "gm:chal", GREEN)],
                   [btn("🏆 برترین‌ها", "gm:top", RED)]])

    MENU_TEXT = (f"<b><i>• — {html.escape(BRAND)} — •</i></b>\n\n<b>🎮 منوی بازی‌ها</b>\n{LN}\n"
                 f"🍀 همه‌ی بازی‌ها شانسیه؛ شانس برد همه برابره\n"
                 "👇 یک بازی رو بزن تا پنلش باز بشه")

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
                P["bet"] = clamp(int(t.replace(",", "")))
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
        if cmd == "chal":
            return await plus_handle(e, cmd, arg, uid, name)

        a = arg.split()
        bet = int(a[0]) if a and a[0].isdigit() else None
        if bet is not None and not MIN_BET <= bet <= MAX_BET:
            return await e.reply(f"مبلغ ورودی حداقل {MIN_BET:,} 💎 باشه" + ("" if MAX_BET >= HARD_CAP else f" و حداکثر {MAX_BET:,}"))
        try:
            P = await open_panel(e.chat_id, e.id, uid, name, cmd, bet)
        except Exception:
            log.exception("open panel failed")
            return await e.reply("⚠️ باز کردن پنل ناموفق بود.")
        if cmd == "duel" and len(a) > 1 and a[1] in ("2", "3"):          # «بازی 20 3» skips the question
            err, _ = await new_lobby(P, int(a[1]))
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
        if what == "chal":
            return await e.answer("🙈 برای ساخت چالش بنویس: چالش 50", alert=True)
        if what not in (*GAME_WORDS.values(), "duel"):
            return await e.answer()
        uid = e.sender_id
        name = name_of(await e.get_sender())
        await db.ensure_wallet(uid, name)
        await e.answer()
        await open_panel(e.chat_id, None, uid, name, what, None)

    @bot.on(events.CallbackQuery(pattern=rb"^gr:"))
    async def cb_replay(e):
        _, game, bet, n = e.data.decode().split(":")
        if game not in (*GAME_WORDS.values(), "duel"):
            return await e.answer()
        uid = e.sender_id
        name = name_of(await e.get_sender())
        await db.ensure_wallet(uid, name)
        await e.answer("🔁 پنل جدید برات باز شد")
        await open_panel(e.chat_id, None, uid, name, game, int(bet), int(n))

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
            P["bet"] = clamp(int(arg))
            P["view"] = "main"
            await e.answer(f"💎 مبلغ ورودی: {P['bet']:,} الماس")
            return await render(P)
        if act == "md" and g == "roulette" and arg in RL_MODES:
            P["mode"] = arg
            await e.answer(RL_MODES[arg] + (" — یه عدد انتخاب کن" if arg == "exact" else ""))
            return await render(P)
        if act == "md" and g == "dice" and arg in ("even", "odd", "exact"):
            P["mode"] = arg
            await e.answer({"even": "زوج", "odd": "فرد", "exact": "حدس دقیق — یه عدد انتخاب کن"}[arg])
            return await render(P)
        if act == "vs" and g == "coin" and arg in ("bot", "user"):
            P["vs"] = arg
            await e.answer("🤖 با ربات" if arg == "bot" else "👤 با کاربر")
            return await render(P)
        if act == "sn" and g == "casino" and arg in ("1", "2"):
            P["n"] = int(arg)
            await e.answer("👤 تک‌نفره" if arg == "1" else "👥 دونفره")
            return await render(P)
        if act == "p":
            if g == "coin" and arg in ("h", "t"):
                P["pick"] = arg
            elif g == "dice" and arg.isdigit() and 1 <= int(arg) <= 6:
                P["pick"] = int(arg)
            elif g == "roulette" and arg.isdigit() and 1 <= int(arg) <= 36:
                P["pick"] = int(arg)
            else:
                return await e.answer()
            await e.answer("✅ انتخاب شد")
            return await render(P)
        if act in ("d2", "d3") and g == "duel":
            async with P["lock"]:
                err, _ = await new_lobby(P, 2 if act == "d2" else 3)
            return await e.answer(err or ("👥 دونفره" if act == "d2" else "👥 سه‌نفره"), alert=bool(err))
        if act == "go" and g == "duel":
            return await e.answer()
        if act == "go":
            async with P["lock"]:
                pvp = (g == "casino" and P["n"] == 2) or (g == "coin" and P["vs"] == "user")
                if pvp:
                    err, _ = await new_lobby(P)
                    return await e.answer(err or "👥 بازی ساخته شد؛ منتظر حریف", alert=bool(err))
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
        async with L["lock"]:
            if L["state"] != "open":
                return await e.answer("بازی شروع شده", alert=True)
            if any(u == uid for u, _ in L["players"]):
                return await e.answer("تو قبلاً پیوستی 😉", alert=True)
            if await db.try_spend(uid, L["bet"], "game_bet" if L["type"] == "duel" else f"{L['type']}_bet") is None:
                return await e.answer("💎 موجودی کافی نیست", alert=True)
            name = name_of(await e.get_sender())
            await db.ensure_wallet(uid, name)
            L["players"].append((uid, name))
            await db.lobby_add(lid, uid)
            start = len(L["players"]) >= L["kind"]
            if start:
                L["state"] = "running"
        if not start:
            await e.answer("پیوستی ✅")
            return await edit_lobby(L, lobby_text(L), lobby_markup(lid, L))
        L["task"].cancel()
        await e.answer("🎲")
        run = {"duel": run_duel, "coin": run_coin_pvp}.get(L["type"], run_casino_pvp)
        return await run(lid)

    @bot.on(events.CallbackQuery(pattern=rb"^lb:"))
    async def cb_bot(e):
        lid = int(e.data.decode().split(":")[1])
        L = lobbies.get(lid)
        if not L:
            return await e.answer("این بازی تموم شده", alert=True)
        if e.sender_id != L["creator"]:
            return await e.answer("فقط سازنده می‌تونه با ربات بازی کنه", alert=True)
        async with L["lock"]:
            if L["state"] != "open" or L["type"] != "duel" or L["kind"] != 2 or len(L["players"]) != 1:
                return await e.answer("الان نمیشه", alert=True)
            L["state"] = "running"
            L["players"].append((0, "🤖 ربات"))
        L["task"].cancel()
        await e.answer("🤖")
        await run_duel(lid)

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
