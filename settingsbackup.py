"""بکاپ و بازگردانی تنظیمات سلف.

`.بکاپ_تنظیمات`      -> a JSON file with every personal setting + keyword auto-replies, sent to Saved Messages
`.بازگردانی_تنظیمات`  -> reply to that file: everything is restored (works on a new login / another account too)

Account-specific or volatile values (chat ids of the private backup group, billing marks, clock-photo ids,
cached base names ...) are NEVER exported or imported, and an imported file is validated key by key.
"""
import io
import json
import re
import time

import db

FORMAT = "xtm-self-settings"
VERSION = 1
MAX_FILE = 1_000_000                    # bytes
MAX_KEYS, MAX_VALUE, MAX_REPLIES = 3000, 20_000, 500

# exact keys / prefixes that belong to the account or to runtime state, not to the user's preferences
SKIP_KEYS = {"backup_chat", "bill_acc", "exp_notified", "low_notified", "base_name", "statbio_base",
             "cpic_base", "cpic_ids", "live_q"}
SKIP_PREFIXES = ("live_", "tmp", "_")
KEY_RE = re.compile(r"^[^\s/\\\x00-\x1f]{1,80}$")          # no spaces / slashes / control chars (emoji keys like «emoji:😀» are fine)


def allowed_key(key: str) -> bool:
    return bool(KEY_RE.match(key)) and key not in SKIP_KEYS and not key.startswith(SKIP_PREFIXES)


async def export_bytes(uid) -> tuple[bytes, int, int]:
    """(json bytes, settings count, replies count)."""
    rows = await db.pool.fetch("SELECT key,value FROM user_settings WHERE user_id=$1 ORDER BY key", uid)
    settings = {r["key"]: r["value"] for r in rows if allowed_key(r["key"]) and len(r["value"]) <= MAX_VALUE}
    replies = [{"keyword": r["keyword"], "response": r["response"]} for r in await db.ar_list(uid)
               if not r["response"].startswith("@msg:")][:MAX_REPLIES]    # media replies point to message ids -> skip
    doc = {"format": FORMAT, "version": VERSION, "created": int(time.time()),
           "settings": settings, "replies": replies}
    return json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8"), len(settings), len(replies)


def parse(raw: bytes):
    """Validate an uploaded file -> (settings dict, replies list) or raise ValueError with a Persian reason."""
    if len(raw) > MAX_FILE:
        raise ValueError("فایل خیلی بزرگه")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ValueError("فایل JSON معتبر نیست")
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise ValueError("این فایل، بکاپ تنظیمات سلف نیست")
    if int(doc.get("version", 0)) > VERSION:
        raise ValueError("این بکاپ برای نسخه‌ی جدیدتری ساخته شده")
    settings, replies = doc.get("settings"), doc.get("replies", [])
    if not isinstance(settings, dict) or not isinstance(replies, list) or len(settings) > MAX_KEYS:
        raise ValueError("ساختار فایل خرابه")
    clean = {}
    for k, v in settings.items():
        if isinstance(k, str) and isinstance(v, (str, int, float)) and allowed_key(k) and len(str(v)) <= MAX_VALUE:
            clean[k] = str(v)
    reps = [(str(r["keyword"])[:100], str(r["response"])[:4000]) for r in replies[:MAX_REPLIES]
            if isinstance(r, dict) and r.get("keyword") and r.get("response")
            and not str(r["response"]).startswith("@msg:")]
    return clean, reps


async def restore(uid, settings: dict, replies: list):
    for k, v in settings.items():
        await db.uset(uid, k, v)
    for kw, resp in replies:
        await db.ar_add(uid, kw, resp)


# ---------------------------------------------------------------- self commands
async def c_settings_backup(c, e, uid, arg):
    await e.edit("⏳ در حال ساخت بکاپ تنظیمات...")
    data, n_set, n_rep = await export_bytes(uid)
    bio = io.BytesIO(data)
    bio.name = f"xtm-self-settings-{time.strftime('%Y%m%d-%H%M')}.json"
    await c.send_file("me", bio, caption=f"💾 بکاپ تنظیمات سلف\n⚙️ {n_set} تنظیم | 💬 {n_rep} پاسخ کلمه‌ای\n"
                                         "برای برگردوندن: روی همین فایل ریپلای کن و بزن `.بازگردانی_تنظیمات`")
    await e.edit(f"✅ بکاپ تنظیمات ({n_set} تنظیم + {n_rep} پاسخ کلمه‌ای) توی Saved Messages ذخیره شد.")


async def c_settings_restore(c, e, uid, arg):
    r = await e.get_reply_message() if e.is_reply else None
    if not r or not r.document:
        return await e.edit("فایل بکاپ تنظیمات رو پیدا کن، روش ریپلای بزن و بنویس `.بازگردانی_تنظیمات`")
    if (r.document.size or 0) > MAX_FILE:
        return await e.edit("⚠️ فایل خیلی بزرگه")
    await e.edit("⏳ در حال بازگردانی...")
    raw = await r.download_media(file=bytes)
    try:
        settings, replies = parse(raw)
    except ValueError as ex:
        return await e.edit(f"⚠️ {ex}")
    await restore(uid, settings, replies)
    await e.edit(f"✅ بازگردانی شد: {len(settings)} تنظیم + {len(replies)} پاسخ کلمه‌ای.\n"
                 "ساعت و قابلیت‌های زمان‌دار طی چند ثانیه‌ی آینده اعمال میشن.")


def install(commands: dict, fa: dict):
    commands["setbackup"], commands["setrestore"] = c_settings_backup, c_settings_restore
    fa["بکاپ_تنظیمات"], fa["بازگردانی_تنظیمات"] = "setbackup", "setrestore"
