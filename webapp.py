"""Telegram Mini App (Web App) for the manager bot.

A tiny aiohttp server that runs in the SAME process as the bot:
    GET  /               -> webapp/index.html (the single-page UI)
    POST /api/<action>   -> JSON API; every call carries Telegram `initData` in the X-Init-Data header and is
                            verified (HMAC-SHA256 with the bot token) before anything happens. The user id comes ONLY
                            from the verified data, so nobody can act on somebody else's account.

Public URL: WEBAPP_URL, else https://$RAILWAY_PUBLIC_DOMAIN. Without a public URL the server still starts but the
buttons are not added (see README -> "مینی‌اپ").
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

import botapi
import catalog
import clockpic
import db
import gameapi
import selfbot
import utils
import wheel

log = logging.getLogger("webapp")
HERE = Path(__file__).parent
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", "8080"))
MAX_AGE = 24 * 3600
H: dict = {}            # hooks injected by main.py: bot_username, support, channel, version, start_buy, send_menu, ...

# command -> which panel of the mini app edits it (these are more than a simple on/off)
PANEL_OF = {"ساعت": "clock", "ساعت_عکس": "cpic", "حالت": "style", "متن_منشی": "afk", "کلمه": "kw",
            "بیو": "profile", "اسم": "profile"}
ACTIONS = {"status": "📊 وضعیت", "bal": "💎 موجودی", "top": "🏆 برترین‌ها", "date": "📅 تاریخ", "ref": "👥 لینک دعوت"}


def public_url() -> str:
    u = os.environ.get("WEBAPP_URL", "").strip().rstrip("/")
    if not u and os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        u = "https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"].strip()
    if u and not u.startswith("http"):
        u = "https://" + u
    return u


# ------------------------------------------------------------------ auth
def validate_init_data(init_data: str, token: str = "", max_age: int = MAX_AGE, now: float | None = None):
    """Returns the Telegram user dict if `initData` is genuine and fresh, else None."""
    token = token or BOT_TOKEN
    if not init_data or not token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:                                           # noqa: BLE001
        return None
    got = pairs.pop("hash", None)
    if not got:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        return None
    try:
        if (now or time.time()) - int(pairs.get("auth_date", "0")) > max_age:
            return None
        user = json.loads(pairs.get("user", ""))
        int(user["id"])
    except Exception:                                           # noqa: BLE001
        return None
    return user


def plain(s: str) -> str:
    return re.sub(r"\*\*|`|__", "", s or "").replace("‌", "‌")


class Err(Exception):
    def __init__(self, msg, code=400):
        self.msg, self.code = msg, code


def need_self(uid):
    c = selfbot.clients.get(uid)
    if not c:
        raise Err("سلف روشن نیست؛ اول از داخل ربات سلف بساز یا الماس شارژ کن")
    return c


# ------------------------------------------------------------------ API handlers (uid, body) -> dict
async def a_state(uid, b, user):
    row = await db.user_get(uid)
    bal = await db.balance(uid)
    sec = await db.remaining_seconds(uid)
    admin = uid in db.ADMINS
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or str(uid)
    ref = f"https://t.me/{H.get('bot_username', '')}?start=ref{uid}"
    return {
        "uid": uid, "name": name, "username": user.get("username") or "", "admin": admin,
        "balance": int(bal), "balance_text": "نامحدود" if admin else db.fmt(bal),
        "toman": 0 if admin else db.value_toman(int(bal)), "left": await db.time_left_text(uid),
        "left_short": await db.time_left_short(uid), "has_self": bool(row), "running": uid in selfbot.clients,
        "selfon": await db.uflag(uid, "selfon", True), "invited": await db.invited_count(uid),
        "ref_link": ref, "ref_bonus": db.REF_BONUS, "support": H.get("support", ""), "channel": H.get("channel", ""),
        "version": H.get("version", ""), "rate": round(db.RATE_PER_HOUR, 2), "pack": db.DIAMONDS_PER_30_DAYS,
        "pack_toman": db.price_toman(db.DIAMONDS_PER_30_DAYS),
        "wheel_ready": admin or await db.wheel_last(uid) < wheel.day_start(),
    }


async def a_features(uid, b, user):
    out = []
    for fi, (folder, feats) in enumerate(catalog.CATS):
        items = []
        for cmd, title, expl, usage, run in feats:
            it = {"cmd": cmd, "title": plain(title), "desc": plain(expl), "usage": [plain(u) for u in usage],
                  "panel": PANEL_OF.get(cmd, ""), "run": run if run in ACTIONS else ""}
            if cmd in catalog.TOGGLE_OF:
                key, default = catalog.TOGGLE_OF[cmd]
                it["toggle"] = True
                it["on"] = await db.uflag(uid, key, default)
            items.append(it)
        out.append({"folder": folder, "items": items})
    return {"folders": out}


async def a_toggle(uid, b, user):
    cmd = b.get("cmd", "")
    if cmd not in catalog.TOGGLE_OF:
        raise Err("این قابلیت دکمه‌ی روشن/خاموش نداره")
    if not await db.user_get(uid):
        raise Err("اول سلف بساز")
    key, default = catalog.TOGGLE_OF[cmd]
    cur = await db.uflag(uid, key, default)
    new = bool(b["on"]) if "on" in b else not cur
    await db.uset_flag(uid, key, new)
    note = ""
    if new and key in ("ttl", "antidel"):
        ok = await selfbot.ensure_backup(uid)
        note = "" if ok else "گپ بکاپ ساخته نشد؛ فعلاً توی Saved Messages ذخیره میشه"
    return {"on": new, "note": note}


async def a_action(uid, b, user):
    key = b.get("key", "")
    if key == "status":
        txt = await selfbot.status_text(uid)
    elif key == "bal":
        txt = f"💎 موجودی: **{await _bal_text(uid)}**"
    elif key == "top":
        rows = await db.top(10)
        txt = "🏆 **برترین‌ها**\n" + ("\n".join(
            f"{i}. {utils.clean_name(r['name'] or str(r['user_id']))} — {r['diamonds']:,} 💎"
            for i, r in enumerate(rows, 1)) or "هنوز کسی نیست")
    elif key == "date":
        txt = selfbot.date_text()
    elif key == "ref":
        txt = (f"👥 لینک دعوت (هر نفر {db.REF_BONUS} 💎):\nhttps://t.me/{H.get('bot_username', '')}?start=ref{uid}\n"
               f"دعوت‌شده‌ها: {await db.invited_count(uid)}")
    else:
        raise Err("نامعتبر")
    return {"text": plain(txt)}


async def _bal_text(uid):
    return "نامحدود" if uid in db.ADMINS else db.fmt(await db.balance(uid))


async def a_selfpower(uid, b, user):
    if not await db.user_get(uid):
        raise Err("اول سلف بساز")
    on = bool(b.get("on"))
    await db.uset_flag(uid, "selfon", on)
    return {"on": on}


# ---- clock (name / family / bio) ----
async def a_clock(uid, b, user):
    fl = await selfbot.clock_flags(uid)
    cur = await db.uget(uid, "clock_font", "0")
    hm = selfbot.datetime.now(selfbot.TZ).strftime("%H:%M")
    return {"flags": fl, "font": cur, "running": uid in selfbot.clients,
            "fonts": [selfbot.clock_str(hm, i) for i in range(len(selfbot.DIGIT_FONTS))]}


async def a_clock_set(uid, b, user):
    c = need_self(uid)
    if "target" in b:
        if b["target"] not in selfbot.CLOCK_TARGETS:
            raise Err("نامعتبر")
        await selfbot.set_clock_target(uid, b["target"], bool(b.get("on")))
    if "font" in b:
        v = b["font"]
        if v != "rand":
            v = int(v)
            if not 0 <= v < len(selfbot.DIGIT_FONTS):
                raise Err("فونت نامعتبر")
        await db.uset(uid, "clock_font", v)
        await selfbot.push_clock(c, uid)
    return await a_clock(uid, b, user)


# ---- clock on the profile photo ----
async def a_cpic(uid, b, user):
    cfg = await selfbot.cpic_cfg(uid)
    return {"cfg": cfg, "running": uid in selfbot.clients, "every_options": list(selfbot.CPIC_EVERY),
            "styles": [{"key": k, "name": n} for k, n in clockpic.STYLES],
            "colors": [{"key": k, "name": v[0], "emoji": v[1], "rgb": list(v[2])} for k, v in clockpic.COLORS.items()]}


async def a_cpic_set(uid, b, user):
    need_self(uid)
    err = ""
    if "style" in b:
        if b["style"] not in dict(clockpic.STYLES):
            raise Err("طرح نامعتبر")
        await db.uset(uid, "cpic_style", b["style"])
    if "color" in b:
        if b["color"] not in clockpic.COLORS:
            raise Err("رنگ نامعتبر")
        await db.uset(uid, "cpic_color", b["color"])
    if "every" in b:
        if int(b["every"]) not in selfbot.CPIC_EVERY:
            raise Err("بازه نامعتبر")
        await db.uset(uid, "cpic_every", str(int(b["every"])))
    if "on" in b:
        err = await selfbot.set_clockpic(uid, bool(b["on"])) or ""
    elif "style" in b or "color" in b:
        asyncio.create_task(selfbot.cpic_refresh(uid))
    res = await a_cpic(uid, b, user)
    res["warn"] = err
    return res


async def a_cpic_preview(uid, b, user):
    c = need_self(uid)
    style = b.get("style") if b.get("style") in dict(clockpic.STYLES) else None
    color = b.get("color") if b.get("color") in clockpic.COLORS else None
    cfg = await selfbot.cpic_cfg(uid)
    base = await selfbot.cpic_get_base(c, uid, capture=not cfg["on"])
    # small preview: render the real thing (same code as the live clock), it is downscaled by the browser
    jpg = await asyncio.get_running_loop().run_in_executor(
        selfbot._POOL, clockpic.render, base, style or cfg["style"], color or cfg["color"], selfbot.datetime.now(selfbot.TZ))
    return {"img": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}


# ---- auto text style ----
async def a_style(uid, b, user):
    modes, font = await selfbot.get_modes(uid), await selfbot.get_autofont(uid)
    return {"modes": modes, "font": font,
            "styles": [{"key": k, "label": selfbot.STYLE_LABEL[k]} for k in selfbot.STYLE_ORDER],
            "fonts": [{"key": k, "sample": f("Abc")} for k, f in utils.FONTS.items()]}


async def a_style_set(uid, b, user):
    if not await db.user_get(uid):
        raise Err("اول سلف بساز")
    if "mode" in b and b["mode"] in selfbot.STYLE_ORDER:
        await selfbot.toggle_mode(uid, b["mode"])
    if "font" in b:
        v, cur = str(b["font"]), await selfbot.get_autofont(uid)
        await db.uset(uid, "autofont", "" if (v == cur or v not in utils.FONTS) else v)
    if b.get("clear"):
        await db.uset(uid, "textmode", "")
        await db.uset(uid, "autofont", "")
    return await a_style(uid, b, user)


# ---- secretary text / keyword replies / profile ----
async def a_afk(uid, b, user):
    return {"text": await db.uget(uid, "afk_text", "سلام 👋 الان آفلاینم، به محض اینکه آنلاین شدم جواب می‌دم."),
            "on": await db.uflag(uid, "afk", False)}


async def a_afk_set(uid, b, user):
    t = str(b.get("text", "")).strip()[:500]
    if not t:
        raise Err("متن خالیه")
    await db.uset(uid, "afk_text", t)
    return await a_afk(uid, b, user)


async def a_kw(uid, b, user):
    rows = await db.ar_list(uid)
    return {"items": [{"kw": r["keyword"], "resp": r["response"]} for r in rows], "on": await db.uflag(uid, "kw", True)}


async def a_kw_set(uid, b, user):
    if not await db.user_get(uid):
        raise Err("اول سلف بساز")
    kw = str(b.get("kw", "")).strip()[:60]
    if b.get("delete"):
        await db.ar_del(uid, kw)
    else:
        resp = str(b.get("resp", "")).strip()[:500]
        if not kw or not resp:
            raise Err("کلمه و جواب هر دو لازمه")
        await db.ar_add(uid, kw, resp)
    return await a_kw(uid, b, user)


async def a_profile_set(uid, b, user):
    c = need_self(uid)
    from telethon import functions
    kw, fl = {}, await selfbot.clock_flags(uid)
    if "bio" in b:
        v = str(b["bio"]).strip()[:70]
        await db.uset(uid, "base_bio", v)
        if fl["bio"]:
            await selfbot.push_clock(c, uid)
        else:
            kw["about"] = v
    if "name" in b:
        v = str(b["name"]).strip()[:64]
        if not v:
            raise Err("اسم خالیه")
        await db.uset(uid, "base_name", v)
        if fl["name"]:
            await selfbot.push_clock(c, uid)
        else:
            kw["first_name"] = v
    if kw:
        await c(functions.account.UpdateProfileRequest(**kw))
    return {"ok": True}


# ---- wallet ----
async def a_wallet(uid, b, user):
    hist = await db.history(uid, 15)
    return {"balance": await _bal_text(uid), "left": await db.time_left_text(uid), "pack": db.DIAMONDS_PER_30_DAYS,
            "pack_toman": db.price_toman(db.DIAMONDS_PER_30_DAYS), "rate": round(db.RATE_PER_HOUR, 2),
            "history": [{"amount": int(r["amount"]), "reason": _reason(r["reason"]), "ts": int(r["ts"].timestamp())
                         if hasattr(r["ts"], "timestamp") else int(r["ts"] or 0)} for r in hist]}


def _reason(r: str) -> str:
    r = r or ""
    if r.startswith("code:"):
        return "🎁 کد هدیه"
    return {"welcome": "🎉 هدیه‌ی ورود", "referral": "👥 زیرمجموعه", "billing": "⏳ هزینه‌ی ساعتی",
            "buy": "💳 خرید الماس"}.get(r, r)


async def a_redeem(uid, b, user):
    code = str(b.get("code", "")).strip()
    if not code:
        raise Err("کد رو وارد کن")
    res = await db.code_redeem(uid, code)
    if isinstance(res, str):
        raise Err({"none": "کد اشتباهه", "used": "قبلاً استفاده کردی", "full": "ظرفیت کد پر شده"}.get(res, "کد نامعتبر"))
    return {"amount": int(res), "balance": await _bal_text(uid)}


async def a_buy(uid, b, user):
    await H["start_buy"](uid)
    return {"ok": True}


async def a_menu(uid, b, user):
    await H["send_menu"](uid)
    return {"ok": True}


# ---- owner only ----
def need_admin(uid):
    if uid not in db.ADMINS:
        raise Err("فقط مالک", 403)


async def a_admin(uid, b, user):
    need_admin(uid)
    codes = await db.code_list(20)
    return {"users": await db.all_users_count(), "selfs": len(selfbot.clients),
            "codes": [{"code": r["code"], "amount": int(r["amount"]), "max": int(r["max_uses"]), "used": int(r["used"])}
                      for r in codes]}


async def a_admin_code(uid, b, user):
    need_admin(uid)
    code = re.sub(r"[^a-z0-9_\-]", "", str(b.get("code", "")).lower())[:32]
    if not code:
        raise Err("کد نامعتبر")
    if b.get("delete"):
        await db.code_delete(code)
    else:
        amount, mx = int(b.get("amount", 0)), int(b.get("max", 1))
        if amount <= 0 or mx <= 0:
            raise Err("مقدار نامعتبر")
        await db.code_create(code, amount, mx)
    return await a_admin(uid, b, user)


async def a_games(uid, b, user):
    admin = uid in db.ADMINS
    return {**gameapi.meta(), "balance": 0 if admin else int(await db.balance(uid)), "balance_text": await _bal_text(uid)}


async def a_game_play(uid, b, user):
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or str(uid)
    try:
        return await gameapi.play(uid, name, b)
    except gameapi.GameErr as e:
        raise Err(e.msg)


# ---- daily lucky wheel
_wheel_locks: dict[int, asyncio.Lock] = {}


async def a_wheel(uid, b, user):
    admin = uid in db.ADMINS
    ready = admin or await db.wheel_last(uid) < wheel.day_start()
    today = None if ready else await db.wheel_today_prize(uid, wheel.day_start())
    return {"ready": ready, "left_s": wheel.seconds_left(), "left_text": wheel.left_text(), "order": wheel.ORDER,
            "odds": [{"v": v, "p": round(p, 1)} for v, p in wheel.odds()], "avg": round(wheel.average_prize()),
            "today": int(today) if today else None, "admin": admin, "balance_text": await _bal_text(uid)}


async def a_wheel_spin(uid, b, user):
    """The SERVER decides (and pays) - the browser only animates to the returned slice."""
    admin = uid in db.ADMINS
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or str(uid)
    lock = _wheel_locks.setdefault(uid, asyncio.Lock())
    if lock.locked():
        raise Err("⏳ چرخش قبلی هنوز تموم نشده")
    async with lock:
        await db.ensure_wallet(uid, utils.clean_name(name))
        prev = 0.0
        if not admin:                                           # owners can spin without limit (testing)
            prev = await db.wheel_claim(uid, wheel.day_start(), time.time())
            if prev is None:
                raise Err(f"امروز چرخوندی؛ گردونه‌ی بعدی {wheel.left_text()} دیگه باز میشه")
        idx, prize = wheel.draw_prize()
        try:
            bal = await db.add(uid, prize, "wheel")
        except Exception:                                       # noqa: BLE001  (give the spin back)
            log.exception("wheel payout failed")
            if not admin:
                await db.wheel_release(uid, prev)
            raise Err("خطا؛ چرخش حساب نشد. دوباره امتحان کن.")
        return {"idx": idx, "prize": prize, "balance": 0 if admin else int(bal), "balance_text": await _bal_text(uid),
                "left_s": wheel.seconds_left(), "left_text": wheel.left_text(), "admin": admin}


ROUTES = {
    "wheel": a_wheel, "wheel_spin": a_wheel_spin,
    "state": a_state, "features": a_features, "toggle": a_toggle, "action": a_action, "selfpower": a_selfpower,
    "clock": a_clock, "clock_set": a_clock_set, "cpic": a_cpic, "cpic_set": a_cpic_set, "cpic_preview": a_cpic_preview,
    "style": a_style, "style_set": a_style_set, "afk": a_afk, "afk_set": a_afk_set, "kw": a_kw, "kw_set": a_kw_set,
    "profile_set": a_profile_set, "wallet": a_wallet, "redeem": a_redeem, "buy": a_buy, "menu": a_menu,
    "admin": a_admin, "admin_code": a_admin_code, "games": a_games, "game_play": a_game_play,
}


# ------------------------------------------------------------------ server
async def index(request):
    return web.FileResponse(HERE / "webapp" / "index.html", headers={"Cache-Control": "no-store"})


async def health(request):
    return web.Response(text="ok")


async def api(request):
    name = request.match_info["name"]
    fn = ROUTES.get(name)
    if not fn:
        return web.json_response({"ok": False, "error": "not found"}, status=404)
    user = validate_init_data(request.headers.get("X-Init-Data", ""))
    if not user:
        return web.json_response({"ok": False, "error": "نشست نامعتبر؛ مینی‌اپ رو از داخل ربات باز کن"}, status=401)
    try:
        body = await request.json() if request.can_read_body else {}
    except Exception:                                           # noqa: BLE001
        body = {}
    uid = int(user["id"])
    try:
        return web.json_response({"ok": True, **(await fn(uid, body or {}, user))})
    except Err as e:
        return web.json_response({"ok": False, "error": e.msg}, status=e.code)
    except Exception as e:                                      # noqa: BLE001
        log.exception("api %s failed", name)
        return web.json_response({"ok": False, "error": f"خطا: {type(e).__name__}"}, status=500)


def make_app() -> web.Application:
    app = web.Application(client_max_size=1_000_000)
    app.router.add_get("/", index)
    app.router.add_get("/healthz", health)
    app.router.add_post("/api/{name}", api)
    return app


async def start(hooks: dict):
    """Start the web server and (if a public URL is known) put the Mini App on the bot's menu button."""
    H.update(hooks)
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    url = public_url()
    log.info("mini app server on :%s  public url: %s", PORT, url or "— (set WEBAPP_URL / generate a Railway domain)")
    if url:
        try:
            await botapi.call("setChatMenuButton", {"menu_button": {"type": "web_app", "text": "🚀 پنل", "web_app": {"url": url}}})
        except Exception as ex:                                 # noqa: BLE001
            log.warning("menu button not set: %s", ex)
    return url
