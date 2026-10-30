"""عضویت اجباری — owners of a self must be members of the required group(s).

* The list lives in the DB (settings key «fj_cfg»), managed by the admin from the bot menu (🔒 عضویت اجباری).
  On the very first run it is seeded with FORCE_JOIN_DEFAULT (default: https://t.me/XTM_SELF).
* Membership is checked with the USER's own session (works for any public group, the bot does not have to be admin).
* Not a member  ->  every self command / auto feature is blocked, and the bot keeps reminding the user until he joins.
* If Telegram can't answer (flood, network...) the user is NOT punished (fail-open).
"""
import json
import logging
import os
import re
import time

from telethon import errors, functions, types

import db

log = logging.getLogger("forcejoin")

DEFAULT = os.getenv("FORCE_JOIN_DEFAULT", "XTM_SELF").strip()
REMIND_MINUTES = max(1, int(os.getenv("FORCE_REMIND_MINUTES", "10")))

CLIENTS: dict = {}                          # uid -> running user client (the same dict object as selfbot.clients)
_cfg_cache = [0.0, None]                    # [loaded_at, cfg]
_member_cache: dict = {}                    # (uid, username) -> (joined, ts)

LINK_RE = re.compile(r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.me/([A-Za-z][A-Za-z0-9_]{3,31})(?:/\d+)?/?(?:[?#].*)?$", re.I)
NAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")


def parse_ref(text: str):
    """'https://t.me/XTM_SELF' | 't.me/XTM_SELF' | '@XTM_SELF' | 'XTM_SELF'  ->  'XTM_SELF'.  Private links -> None."""
    t = (text or "").strip()
    m = LINK_RE.match(t) or NAME_RE.match(t)
    if not m or m.group(1).lower() in ("joinchat", "addlist", "c", "s"):
        return None
    return m.group(1)


async def load() -> dict:
    if _cfg_cache[1] is not None and time.time() - _cfg_cache[0] < 20:
        return _cfg_cache[1]
    raw = await db.get("fj_cfg")
    cfg = None
    if raw:
        try:
            cfg = json.loads(raw)
        except Exception:
            cfg = None
    if cfg is None:                                     # first run: seed with the default group
        u = parse_ref(DEFAULT)
        cfg = {"on": bool(u), "chats": [{"u": u, "t": u}] if u else []}
        await db.set_("fj_cfg", json.dumps(cfg, ensure_ascii=False))
    _cfg_cache[0], _cfg_cache[1] = time.time(), cfg
    return cfg


async def save(cfg: dict):
    await db.set_("fj_cfg", json.dumps(cfg, ensure_ascii=False))
    _cfg_cache[0], _cfg_cache[1] = time.time(), cfg


def forget(uid=None):
    """Drop cached membership answers (after «✅ عضو شدم»)."""
    for k in list(_member_cache):
        if uid is None or k[0] == uid:
            _member_cache.pop(k, None)


async def _joined(uid, c, username: str) -> bool:
    k = (uid, username.lower())
    hit = _member_cache.get(k)
    if hit and time.time() - hit[1] < (120 if hit[0] else 15):
        return hit[0]
    try:
        ent = await c.get_input_entity(username)
        res = await c(functions.channels.GetParticipantRequest(channel=ent, participant=types.InputPeerSelf()))
        joined = not isinstance(res.participant, (types.ChannelParticipantLeft, types.ChannelParticipantBanned))
    except errors.UserNotParticipantError:
        joined = False
    except Exception as ex:                               # can't verify -> do not punish
        log.info("membership check failed for %s/%s: %s", uid, username, type(ex).__name__)
        joined = True
    _member_cache[k] = (joined, time.time())
    return joined


async def missing(uid, c) -> list:
    """Required chats the user has NOT joined yet (empty list = fine / feature off / admin / no client)."""
    if uid in db.ADMINS or c is None:
        return []
    cfg = await load()
    if not cfg.get("on") or not cfg.get("chats"):
        return []
    return [ch for ch in cfg["chats"] if not await _joined(uid, c, ch["u"])]


async def ok(uid, c) -> bool:
    return not await missing(uid, c)


async def usable(uid) -> bool:
    """Self may act: paid time left AND (if enabled) the user joined every required group."""
    return await db.user_is_active(uid) and await ok(uid, CLIENTS.get(uid))


# ---------------------------------------------------------------- texts / buttons
def link(ch) -> str:
    return f"https://t.me/{ch['u']}"


def self_text(chats) -> str:
    """Shown by the self itself (edit of the command message)."""
    body = "\n".join(f"• {link(c)}" for c in chats)
    return ("⛔ **عضویت اجباری**\nبرای استفاده از سلف اول باید عضو گپ ما بشی:\n"
            f"{body}\n\nبعد از عضویت دوباره دستور رو بزن ✅")


def bot_text(chats) -> str:
    """Reminder / gate message sent by the bot (HTML)."""
    body = "\n".join(f"• <a href=\"{link(c)}\">{c.get('t') or c['u']}</a>" for c in chats)
    return ("<blockquote>⛔ عضویت اجباری</blockquote>\nبرای استفاده از سلف باید عضو گپ ما بشی؛ "
            "تا عضو نشی سلفت کار نمی‌کنه 🚫\n\n" + body +
            "\n\nبعد از عضویت دکمه‌ی «✅ عضو شدم» رو بزن.")
