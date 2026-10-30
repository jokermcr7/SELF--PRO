"""Game extras (v1.8) for the special game group:

  🎰 جکپات روزانه   1% of every wager flows into a pot; every night one player of that day is drawn (random.org),
                     each game played = 1 ticket (more games = more tickets, but every ticket has the same chance).
  🏁 تورنمنت هفتگی   weekly leaderboard (win = 3 points, game played = 1 point); top 3 get diamonds on Saturday.
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
BRAND = os.getenv("BRAND", "Vip MMD")
JACKPOT_PERCENT = 1                                                   # % of each wager that feeds the jackpot
PRIZES = [int(x) for x in os.getenv("TOURNEY_PRIZES", "500,300,150").replace("،", ",").split(",") if x.strip().isdigit()][:3]
CH_N = 9                                                              # secret numbers 1..9 (odd => perfectly fair)
CH_JOIN_TIMEOUT, CH_PICK_TIMEOUT = 180, 90

_ids = itertools.count(int(time.time()) % 10 ** 8)
challenges: dict[int, dict] = {}


def esc(s):
    return html.escape(utils.clean_name(str(s)))


def today() -> str:
    return datetime.now(TZ).date().isoformat()


def week_key(d=None) -> str:
    """Tournament weeks start on Saturday (Iran)."""
    d = d or datetime.now(TZ).date()
    return (d - timedelta(days=(d.weekday() + 2) % 7)).isoformat()


def tables_sql() -> str:
    return """
    CREATE TABLE IF NOT EXISTS jp_tickets(day TEXT, user_id BIGINT, name TEXT, n INT NOT NULL DEFAULT 0, PRIMARY KEY(day, user_id));
    CREATE TABLE IF NOT EXISTS tourney(week TEXT, user_id BIGINT, name TEXT, games INT NOT NULL DEFAULT 0,
        wins INT NOT NULL DEFAULT 0, wagered BIGINT NOT NULL DEFAULT 0, PRIMARY KEY(week, user_id));
    """


# ------------------------------------------------------------------ recording (called after every game)
async def record(uid, name, bet, won):
    """One finished game of a human player: jackpot ticket + pot, tournament points."""
    if not uid:
        return
    try:
        name = utils.clean_name(name or str(uid))
        await db.pool.execute(
            "INSERT INTO jp_tickets(day,user_id,name,n) VALUES($1,$2,$3,1) "
            "ON CONFLICT(day,user_id) DO UPDATE SET n=jp_tickets.n+1, name=EXCLUDED.name", today(), uid, name)
        await db.pool.execute(
            "INSERT INTO tourney(week,user_id,name,games,wins,wagered) VALUES($1,$2,$3,1,$4,$5) "
            "ON CONFLICT(week,user_id) DO UPDATE SET games=tourney.games+1, wins=tourney.wins+$4, "
            "wagered=tourney.wagered+$5, name=EXCLUDED.name", week_key(), uid, name, 1 if won else 0, int(bet))
        # pot is kept in 1/100 diamonds so 1% of a 20-diamond bet is not lost to rounding
        await db.pool.execute(
            "INSERT INTO settings(key,value) VALUES('jp_pool_cent',$1) "
            "ON CONFLICT(key) DO UPDATE SET value=(settings.value::bigint + $2)::text",
            str(int(bet) * JACKPOT_PERCENT), int(bet) * JACKPOT_PERCENT)
    except Exception:                                                  # noqa: BLE001
        log.exception("game record failed")


async def pool_diamonds() -> int:
    return int(await db.get("jp_pool_cent", "0") or 0) // 100


def _left(dt_end) -> str:
    sec = max(0, int((dt_end - datetime.now(TZ)).total_seconds()))
    return f"{sec // 3600} ساعت و {sec % 3600 // 60} دقیقه"


async def jackpot_text(uid=None) -> str:
    rows = await db.pool.fetch("SELECT user_id,name,n FROM jp_tickets WHERE day=$1 ORDER BY n DESC", today())
    total = sum(r["n"] for r in rows)
    mine = next((r["n"] for r in rows if r["user_id"] == uid), 0)
    end = (datetime.now(TZ) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    chance = f"{mine / total * 100:.1f}٪" if total and mine else "—"
    return (f"<b><i>• — {html.escape(BRAND)} — •</i></b>\n\n<b>🎰 جکپات روزانه</b>\n{LN}\n"
            f"💰 صندوق الان: <b>{await pool_diamonds():,} 💎</b>\n"
            f"🎟 بلیط‌های امروز: <b>{total}</b> | بلیط‌های تو: <b>{mine}</b> (شانس {chance})\n"
            f"⏳ قرعه‌کشی: <b>{_left(end)}</b> دیگه (ساعت ۰۰:۰۰)\n{LN}\n"
            f"🍀 هر بازی = یک بلیط؛ همه‌ی بلیط‌ها شانس برابر دارن و برنده با random.org انتخاب میشه. "
            f"{JACKPOT_PERCENT}٪ از هر شرط وارد صندوق میشه.")


async def tourney_text(uid=None) -> str:
    wk = week_key()
    rows = await db.pool.fetch(
        "SELECT user_id,name,games,wins,(wins*3+games) AS pts FROM tourney WHERE week=$1 ORDER BY pts DESC, wins DESC LIMIT 10", wk)
    medals = ["🥇", "🥈", "🥉"] + ["▫️"] * 7
    lines = [f"{medals[i]} {esc(r['name'])} — <b>{r['pts']}</b> امتیاز ({r['wins']} برد از {r['games']} بازی)"
             for i, r in enumerate(rows)]
    me = await db.pool.fetchrow("SELECT games,wins FROM tourney WHERE week=$1 AND user_id=$2", wk, uid) if uid else None
    nxt = datetime.fromisoformat(wk).replace(tzinfo=TZ) + timedelta(days=7)
    prizes = " | ".join(f"{medals[i]} {p:,} 💎" for i, p in enumerate(PRIZES)) or "—"
    mine = f"\n👤 امتیاز تو: <b>{me['wins'] * 3 + me['games']}</b>" if me else ""
    return (f"<b><i>• — {html.escape(BRAND)} — •</i></b>\n\n<b>🏁 تورنمنت هفتگی</b>\n{LN}\n"
            f"🎁 جایزه‌ها: {prizes}\n⏳ پایان: <b>{_left(nxt)}</b> دیگه (شنبه ۰۰:۰۰)\n{LN}\n"
            + ("\n".join(lines) or "هنوز کسی بازی نکرده؛ اولین نفر باش! 🔥") + mine +
            "\n\n📌 هر بازی ۱ امتیاز | هر برد ۳ امتیاز")


# ------------------------------------------------------------------ nightly / weekly payouts
async def _payout_loop(bot, game_chat, rnd_ints):
    while True:
        try:
            await asyncio.sleep(30)
            chat = await game_chat()
            # ---- jackpot: every finished day with tickets
            for r in await db.pool.fetch("SELECT DISTINCT day FROM jp_tickets WHERE day < $1 ORDER BY day", today()):
                day = r["day"]
                if not await db.claim_event(f"jp:{day}"):
                    continue
                rows = await db.pool.fetch("SELECT user_id,name,n FROM jp_tickets WHERE day=$1 ORDER BY user_id", day)
                total = sum(x["n"] for x in rows)
                amount = await pool_diamonds()
                if not rows or amount < 1:
                    await db.pool.execute("DELETE FROM jp_tickets WHERE day=$1", day)
                    continue
                ticket = (await rnd_ints(1, 1, total))[0]
                acc, win = 0, rows[0]
                for x in rows:
                    acc += x["n"]
                    if ticket <= acc:
                        win = x
                        break
                await db.pool.execute("UPDATE settings SET value=(value::bigint - $1)::text WHERE key='jp_pool_cent'", amount * 100)
                await db.add(win["user_id"], amount, "jackpot_win")
                await db.pool.execute("DELETE FROM jp_tickets WHERE day=$1", day)
                if chat:
                    await botapi.send_text(chat, (
                        f"<b><i>• — {html.escape(BRAND)} — •</i></b>\n\n<b>🎰 برنده‌ی جکپات {day}</b>\n{LN}\n"
                        f"🏆 <a href=\"tg://user?id={win['user_id']}\">{esc(win['name'])}</a>\n"
                        f"💰 جایزه: <b>{amount:,} 💎</b>\n🎟 بلیط‌های برنده: {win['n']} از {total} (قرعه: random.org)"))
            # ---- tournament: every finished week
            for r in await db.pool.fetch("SELECT DISTINCT week FROM tourney WHERE week < $1 ORDER BY week", week_key()):
                wk = r["week"]
                if not await db.claim_event(f"tr:{wk}"):
                    continue
                rows = await db.pool.fetch(
                    "SELECT user_id,name,games,wins,(wins*3+games) AS pts FROM tourney WHERE week=$1 "
                    "ORDER BY pts DESC, wins DESC LIMIT 3", wk)
                lines = []
                for i, x in enumerate(rows):
                    if i < len(PRIZES) and PRIZES[i] > 0:
                        await db.add(x["user_id"], PRIZES[i], "tourney_prize")
                    lines.append(f"{['🥇', '🥈', '🥉'][i]} <a href=\"tg://user?id={x['user_id']}\">{esc(x['name'])}</a> — "
                                 f"{x['pts']} امتیاز" + (f" ← <b>{PRIZES[i]:,} 💎</b>" if i < len(PRIZES) else ""))
                await db.pool.execute("DELETE FROM tourney WHERE week=$1", wk)
                if chat and lines:
                    await botapi.send_text(chat, f"<b><i>• — {html.escape(BRAND)} — •</i></b>\n\n<b>🏁 نتیجه‌ی تورنمنت هفته</b>\n{LN}\n"
                                                 + "\n".join(lines) + "\n\n🔥 تورنمنت جدید شروع شد!")
        except asyncio.CancelledError:
            raise
        except Exception:                                              # noqa: BLE001
            log.exception("payout loop")
            await asyncio.sleep(60)


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
                f"👤 سازنده: {esc(C['p'][0][1])}\n")

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
            for i, (u, nm) in enumerate(C["p"]):
                await record(u, nm, C["bet"], i == win)
            text = (head(C) + f"\n🔓 عددهای مخفی:\n{'🏆' if win == 0 else '▫️'} {esc(n1)}: <b>{a}</b>\n"
                    f"{'🏆' if win == 1 else '▫️'} {esc(n2)}: <b>{b}</b>\n🎯 عدد هدف: <b>{t}</b>\n"
                    f"<i>({how}{' | انتخاب‌های ناقص خودکار پر شد' if C.get('auto') else ''})</i>\n\n"
                    f"🏆 برنده: <b>{esc(wname)}</b> | 💎 جایزه: <b>{pz:,}</b>")
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
        """Group commands: جکپات | تورنمنت | چالش [مبلغ]. Returns True when handled."""
        if cmd == "jackpot":
            await botapi.send_text(e.chat_id, await jackpot_text(uid), None, reply_to=e.id)
        elif cmd == "tourney":
            await botapi.send_text(e.chat_id, await tourney_text(uid), None, reply_to=e.id)
        elif cmd == "chal":
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
                    C["chat"], C["msg"], head(C) + f"👥 حریف: {esc(name)}\n\n🙈 حالا هر دو نفر یه عدد مخفی بزنید "
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
                return await botapi.edit_banner(C["chat"], C["msg"], head(C) + f"👥 حریف: {esc(C['p'][1][1])}\n\n"
                                                f"🔒 یک نفر عددش رو قفل کرد؛ منتظر نفر دوم ...", num_markup(cid))

    asyncio.get_running_loop().create_task(_payout_loop(bot, H["game_chat"], rnd_ints))
    return handle
