"""Games inside the Mini App (against the bot). Everything is decided on the SERVER - the browser only animates.

Every outcome comes from random.org (fallback: OS crypto RNG); against the bot the player's chance is PLAYER_WIN_PERCENT (default 44%).
Players vs players still happens inside the game group (see groupgames.py)."""
import asyncio
import os
import time

import db
import gameplus
import groupgames as G
import utils


class GameErr(Exception):
    def __init__(self, msg):
        self.msg = msg


_locks: dict[int, asyncio.Lock] = {}
_last: dict[int, float] = {}
GAMES = ("dice", "coin", "roulette", "casino", "duel", "chal")


def _prize(bet):
    pot = bet * 2
    return pot - pot * G.FEE // 100


def _sym(r):
    return [G.SYM[i] for i in r]


_hit = G.hit
PARITY, EXACT, SIDE, EXACT36, CASINO = 2.0, 6.0, 2.0, 36.0, 2.0     # mini-app multipliers (the edge is in the win chance)


# ---------------------------------------------------------------- games: (uid, bet, body) -> dict(win, pay, ...)
async def g_dice(uid, bet, b):
    mode = b.get("mode", "even")
    if mode not in ("even", "odd", "exact"):
        raise GameErr("حالت نامعتبر")
    pick = int(b.get("pick") or 0)
    if mode == "exact" and not 1 <= pick <= 6:
        raise GameErr("یه عدد از ۱ تا ۶ انتخاب کن")
    v, win = await G.roll_biased(range(1, 7), lambda x: x == pick if mode == "exact" else (x % 2 == 0) == (mode == "even"))
    mult = EXACT if mode == "exact" else PARITY
    return {"win": win, "pay": G.pay_of(bet, mult) if win else 0, "v": v, "mode": mode, "pick": pick, "mult": mult}


async def g_coin(uid, bet, b):
    pick = b.get("pick", "h")
    if pick not in ("h", "t"):
        raise GameErr("شیر یا خط؟")
    win = await _hit(G.COIN_WIN_P)
    res = pick if win else ("t" if pick == "h" else "h")
    return {"win": win, "pay": bet * 2 if win else 0, "res": res, "pick": pick}


async def g_roulette(uid, bet, b):
    mode = b.get("mode", "red")
    if mode not in G.RL_MODES:
        raise GameErr("حالت نامعتبر")
    pick = int(b.get("pick") or 0)
    if mode == "exact" and not 1 <= pick <= 36:
        raise GameErr("یه عدد از ۱ تا ۳۶ انتخاب کن")
    v, win = await G.roll_biased(range(1, 37), lambda x: G.rl_win(mode, pick, x))
    red = v in G.RL_RED
    mult = EXACT36 if mode == "exact" else SIDE
    return {"win": win, "pay": G.pay_of(bet, mult) if win else 0, "v": v, "red": red, "mode": mode, "pick": pick, "mult": mult}


async def g_casino(uid, bet, b):
    win = await _hit(G.PLAYER_WIN_PERCENT / 100)             # outcome first (random.org) ...
    while True:                                              # ... then two spins that match it
        mv, bv = G._rng.randint(1, 64), G._rng.randint(1, 64)
        mr, _m, ml, mk = G.slot_eval(mv)
        br, _m2, bl, bk = G.slot_eval(bv)
        if mk != bk and (mk > bk) == win:
            break
    return {"win": win, "pay": G.pay_of(bet, CASINO) if win else 0,
            "rounds": [{"me": _sym(mr), "bot": _sym(br), "me_label": ml, "bot_label": bl}]}


async def g_duel(uid, bet, b):
    win = await _hit(G.BOT_WIN_PERCENT / 100)
    me, bot = G.bot_duel_numbers(win)
    return {"win": win, "pay": _prize(bet) if win else 0, "me": me, "bot": bot, "tax": bet * 2 * G.FEE // 100}


async def g_chal(uid, bet, b):
    N = gameplus.CH_N
    pick = int(b.get("pick") or 0)
    if not 1 <= pick <= N:
        raise GameErr(f"یه عدد مخفی از ۱ تا {N} انتخاب کن")
    win = await _hit(G.PLAYER_WIN_PERCENT / 100)
    while True:                                              # bot number + target that produce the decided outcome
        bot, t = G._rng.randint(1, N), G._rng.randint(1, N)
        dm, dbt = gameplus._dist(pick, t), gameplus._dist(bot, t)
        if dm != dbt and (dm < dbt) == win:
            break
    return {"win": win, "pay": _prize(bet) if win else 0, "pick": pick, "bot": bot, "target": t, "how": "near",
            "tax": bet * 2 * G.FEE // 100}


PLAY = {"dice": g_dice, "coin": g_coin, "roulette": g_roulette, "casino": g_casino, "duel": g_duel, "chal": g_chal}


async def play(uid, name, body):
    game = body.get("game")
    if game not in PLAY:
        raise GameErr("بازی نامعتبر")
    try:
        bet = int(body.get("bet", 0))
    except (TypeError, ValueError):
        raise GameErr("مبلغ نامعتبر")
    if not G.MIN_BET <= bet <= G.MAX_BET:
        raise GameErr(f"مبلغ ورودی حداقل {G.MIN_BET:,} الماس باشه" + ("" if G.MAX_BET >= G.HARD_CAP else f" و حداکثر {G.MAX_BET:,}"))
    lock = _locks.setdefault(uid, asyncio.Lock())
    if lock.locked():
        raise GameErr("⏳ بازی قبلی هنوز تموم نشده")
    async with lock:
        if time.time() - _last.get(uid, 0) < 1.0:
            raise GameErr("یه لحظه صبر کن")
        _last[uid] = time.time()
        await db.ensure_wallet(uid, utils.clean_name(name))
        if await db.try_spend(uid, bet, f"{game}_bet") is None:
            raise GameErr("💎 موجودی کافی نیست")
        try:
            res = await PLAY[game](uid, bet, body)
        except Exception as ex:                              # noqa: BLE001  (refund on ANY failure)
            await db.add(uid, bet, f"{game}_refund")
            if isinstance(ex, GameErr):
                raise
            raise GameErr("خطا توی بازی؛ ورودیت برگشت داده شد")
        pay = res.pop("pay")
        bal = await db.add(uid, pay, f"{game}_win") if pay else await db.balance(uid)
        admin = uid in db.ADMINS
        return {"game": game, "bet": bet, "net": pay - bet, "pay": pay, "balance": int(bal) if not admin else 0,
                "balance_text": "نامحدود" if admin else db.fmt(bal), **res}


def meta():
    return {"min_bet": G.MIN_BET, "max_bet": 0 if G.MAX_BET >= G.HARD_CAP else G.MAX_BET, "fee": G.FEE, "win_pct": round(G.PLAYER_WIN_PERCENT),
            "game_link": os.getenv("GAME_GROUP_LINK", "").strip(), "ch_n": gameplus.CH_N,
            "mult": {"dice_parity": PARITY, "dice_exact": EXACT, "coin": 2, "roulette_side": SIDE,
                     "roulette_exact": EXACT36, "casino": CASINO}}
