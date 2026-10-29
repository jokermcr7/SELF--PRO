"""Tiny Bot API (HTTP) client.

Telethon 1.36 cannot send the new *colored* inline buttons (green / red / blue). The Bot API can
(`style` field), so game messages are sent/edited through HTTP; button clicks still arrive through
Telethon as usual. Message ids and chat ids are identical in both APIs.
"""
import json
import logging
import os

import aiohttp

import assets

log = logging.getLogger("botapi")
TOKEN = os.environ.get("BOT_TOKEN", "")
_session: aiohttp.ClientSession | None = None
_photo_id: str | None = None   # Bot API file_id of the banner after the first upload

GREEN, RED, BLUE = "success", "danger", "primary"


class BotApiError(Exception):
    pass


def btn(text: str, data: str, style: str | None = None) -> dict:
    d = {"text": text, "callback_data": data}
    if style:
        d["style"] = style
    return d


def kb(rows) -> dict:
    return {"inline_keyboard": rows}


def _s() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    return _session


async def call(method: str, params: dict | None = None, form: aiohttp.FormData | None = None):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    async with _s().post(url, json=params if form is None else None, data=form) as r:
        data = await r.json(content_type=None)
    if not data.get("ok"):
        raise BotApiError(data.get("description", "unknown error"))
    return data["result"]


async def send_banner(chat_id, caption: str, markup: dict, reply_to: int | None = None) -> dict:
    """Send the banner photo with an HTML caption + colored buttons. Returns the Bot API message."""
    global _photo_id
    extra = {"parse_mode": "HTML", "reply_markup": markup}
    if reply_to:
        extra["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    if not assets.BANNER_PATH:
        return await call("sendMessage", {"chat_id": chat_id, "text": caption, **extra})
    if _photo_id:
        try:
            return await call("sendPhoto", {"chat_id": chat_id, "photo": _photo_id, "caption": caption, **extra})
        except BotApiError as ex:
            log.warning("cached banner failed (%s), re-uploading", ex)
            _photo_id = None
    form = aiohttp.FormData()
    form.add_field("chat_id", str(chat_id))
    form.add_field("caption", caption)
    form.add_field("parse_mode", "HTML")
    form.add_field("reply_markup", json.dumps(markup))
    if reply_to:
        form.add_field("reply_parameters",
                       json.dumps({"message_id": reply_to, "allow_sending_without_reply": True}))
    with open(assets.BANNER_PATH, "rb") as f:
        form.add_field("photo", f.read(), filename=os.path.basename(assets.BANNER_PATH),
                       content_type="image/jpeg")
    msg = await call("sendPhoto", form=form)
    try:
        _photo_id = msg["photo"][-1]["file_id"]
    except Exception:
        pass
    return msg


async def edit_banner(chat_id, message_id: int, caption: str, markup: dict | None = None):
    """Edit caption (photo message) or text (when there is no banner). Silently ignores 'not modified'."""
    params = {"chat_id": chat_id, "message_id": message_id, "parse_mode": "HTML",
              "reply_markup": markup if markup is not None else {"inline_keyboard": []}}
    try:
        if assets.BANNER_PATH:
            await call("editMessageCaption", {**params, "caption": caption})
        else:
            await call("editMessageText", {**params, "text": caption})
    except BotApiError as ex:
        if "not modified" not in str(ex):
            raise


# ------------------------------------------------------------------ helpers for the styled UI
def url_btn(text: str, url: str, style: str | None = None) -> dict:
    d = {"text": text, "url": url}
    if style:
        d["style"] = style
    return d


def pack_inline_id(msg_id) -> str:
    """Telethon InputBotInlineMessageID(64) -> the `inline_message_id` string the Bot API wants."""
    import base64
    import struct
    if hasattr(msg_id, "owner_id"):
        raw = struct.pack("<iqiq", msg_id.dc_id, msg_id.owner_id, msg_id.id, msg_id.access_hash)
    else:
        raw = struct.pack("<iqq", msg_id.dc_id, msg_id.id, msg_id.access_hash)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


async def send_text(chat_id, text: str, markup: dict | None = None, reply_to: int | None = None) -> dict:
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
    if reply_to:
        p["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    if markup:
        p["reply_markup"] = markup
    return await call("sendMessage", p)


async def edit_any(target: dict, text: str, markup: dict | None = None):
    """Edit a bot message (photo caption or plain text, chat or inline). target = {chat_id, message_id}
    or {inline_message_id}. 'Not modified' is ignored."""
    base = {**target, "parse_mode": "HTML", "reply_markup": markup if markup is not None else {"inline_keyboard": []}}
    order = [("editMessageCaption", "caption"), ("editMessageText", "text")]
    if not assets.BANNER_PATH:
        order.reverse()
    last = None
    for method, field in order:
        try:
            return await call(method, {**base, field: text})
        except BotApiError as ex:
            s = str(ex)
            if "not modified" in s:
                return None
            if "no caption" in s or "no text" in s:
                last = ex
                continue
            raise
    if last:
        raise last


async def photo_id(chat_id) -> str | None:
    """Bot API file_id of the banner (uploads it once to `chat_id`, then deletes that message)."""
    global _photo_id
    if _photo_id or not assets.BANNER_PATH:
        return _photo_id
    try:
        m = await send_banner(chat_id, ".", {"inline_keyboard": []})
        try:
            await call("deleteMessage", {"chat_id": chat_id, "message_id": m["message_id"]})
        except BotApiError:
            pass
    except BotApiError:
        log.exception("banner upload failed")
    return _photo_id


def to_telethon(markup: dict):
    """Plain (uncolored) Telethon buttons from a Bot API markup - fallback when a styled edit fails."""
    from telethon import Button
    rows = []
    for r in markup.get("inline_keyboard", []):
        row = []
        for b in r:
            row.append(Button.url(b["text"], b["url"]) if "url" in b
                       else Button.inline(b["text"], b.get("callback_data", "noop").encode()))
        rows.append(row)
    return rows


def balance_card(diamonds: int, fmt_amount: str, value: str, who_html: str = "") -> tuple[str, dict]:
    """The 'your balance' message: red diamonds button + green toman/dollar value button."""
    text = f"موجودی {who_html}".strip() if who_html else "موجودی شما"
    return text, kb([[btn(f"{fmt_amount} الماس 💎", "gi:b", RED)], [btn(f"💰 {value}", "gi:v", GREEN)]])
