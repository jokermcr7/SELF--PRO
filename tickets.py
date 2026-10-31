"""🎫 تیکت پشتیبانی داخل ربات.

User:   menu «🎫 تیکت پشتیبانی» -> list of his tickets / new ticket / reply / close
Admin:  menu «🎫 تیکت‌ها» -> open tickets (paged) -> read, reply, close, reopen
Every message (text, photo or file) is stored and relayed as a COPY, so the admin's identity is never shown to the
user (and the user's text is HTML-escaped everywhere). Max 3 open tickets per user; closed tickets accept no messages.
"""
import html
import logging
import re

from telethon import Button, events

import db
import utils

log = logging.getLogger("tickets")

PENDING = ("tknew", "tkre", "tkad")           # pending-input kinds handled by this module
MAX_LEN = 1500
PAGE = 8


def esc(x):
    return html.escape(str(x), quote=False)


def clip(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


CAPTION_LIMIT = 950                           # the menu is a photo message: its caption may hold only 1024 visible chars


def visible_len(t: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", t)))


def convo_text(t, msgs, for_admin: bool) -> str:
    st = "🟢 باز" if t["status"] == "open" else "✅ بسته"
    who = f"\n👤 کاربر: <a href=\"tg://user?id={t['user_id']}\">{t['user_id']}</a>" if for_admin else ""
    head = f"<blockquote>🎫 تیکت #{t['id']} — {st}</blockquote>\n📝 {esc(clip(t['subject'], 40))}{who}\n\n"
    lines = []
    for m in msgs:
        mine = (m["from_admin"] and for_admin) or (not m["from_admin"] and not for_admin)
        tag = ("🛠 تو" if for_admin else "👤 تو") if mine else ("🛠 پشتیبانی" if m["from_admin"] else "👤 کاربر")
        lines.append((tag, m["body"], bool(m["media"])))

    def render(items, cut):
        return head + "\n\n".join(f"<b>{tag}</b>: {esc(clip(body, cut))}" + (" 📎" if media else "")
                                  for tag, body, media in items)

    items = lines[-6:]
    for cut in (300, 200, 140, 100):                         # first shorten the messages ...
        if visible_len(render(items, cut)) <= CAPTION_LIMIT:
            return render(items, cut)
    while len(items) > 1:                                    # ... then drop the oldest ones
        items = items[1:]
        if visible_len(render(items, 100)) <= CAPTION_LIMIT:
            return render(items, 100)
    cut = 100
    while cut > 30 and visible_len(render(items, cut)) > CAPTION_LIMIT:
        cut -= 30
    return render(items, cut)


def register(bot, ctx):
    """ctx: B, G, R, ADMINS, pending, edit_view, send_menu   (provided by main.py)"""
    B, G, R, ADMINS, pending = ctx.B, ctx.G, ctx.R, ctx.ADMINS, ctx.pending

    # ------------------------------------------------------------ views
    async def user_list_view(uid):
        rows_db = await db.tickets_of_user(uid)
        text = ("<blockquote>🎫 تیکت پشتیبانی</blockquote>\nمشکل یا سوالت رو برامون بنویس؛ پاسخ همین‌جا توی ربات میاد.\n\n"
                + ("\n".join(f"{'🟢' if r['status'] == 'open' else '✅'} #{r['id']} — {esc(clip(r['subject'], 35))}"
                             for r in rows_db) or "هنوز تیکتی نداری."))
        rows = [[B(f"{'🟢' if r['status'] == 'open' else '✅'} #{r['id']} {clip(r['subject'], 22)}", f"tk:v:{r['id']}", G)]
                for r in rows_db]
        rows.append([B("➕ تیکت جدید", "tk:new", G)])
        rows.append([B("بازگشت", "m:menu", R)])
        return text, rows

    async def user_ticket_view(t):
        text = convo_text(t, await db.ticket_msgs(t["id"]), False)
        rows = ([[B("✍️ پاسخ", f"tk:r:{t['id']}", G), B("🔒 بستن تیکت", f"tk:c:{t['id']}", R)]]
                if t["status"] == "open" else [])
        rows.append([B("بازگشت", "tk:my", R)])
        return text, rows

    async def admin_list_view(page):
        items, more = await db.tickets_open(page * PAGE, PAGE)
        total = await db.tickets_open_total()
        text = (f"<blockquote>🎫 تیکت‌های باز | فقط مدیر</blockquote>\nتعداد: <b>{total}</b>\n\n"
                + ("\n".join(f"🟢 #{r['id']} — <code>{r['user_id']}</code> — {esc(clip(r['subject'], 30))}" for r in items)
                   or "تیکت بازی نیست 🎉"))
        rows = [[B(f"#{r['id']} · {r['user_id']} · {clip(r['subject'], 16)}", f"tka:v:{r['id']}", G)] for r in items]
        nav = []
        if page > 0:
            nav.append(B("‹ قبلی", f"tka:l:{page - 1}", R))
        if more:
            nav.append(B("بعدی ›", f"tka:l:{page + 1}", R))
        if nav:
            rows.append(nav)
        rows.append([B("بازگشت", "m:menu", R)])
        return text, rows

    async def admin_ticket_view(t):
        text = convo_text(t, await db.ticket_msgs(t["id"], 8), True)
        if t["status"] == "open":
            rows = [[B("✉️ پاسخ", f"tka:r:{t['id']}", G), B("🔒 بستن", f"tka:c:{t['id']}", R)]]
        else:
            rows = [[B("🔓 باز کردن دوباره", f"tka:o:{t['id']}", G)]]
        rows.append([B("بازگشت به لیست", "tka:l:0", R)])
        return text, rows

    # ------------------------------------------------------------ relaying (copies, no forward headers)
    async def relay(chat, header, body, msg, buttons):
        try:
            text = f"{header}\n\n{esc(clip(body, MAX_LEN))}"
            if msg is not None and msg.media and not getattr(msg.media, "webpage", None):
                return await bot.send_file(chat, msg.media, caption=text[:1000], buttons=buttons, parse_mode="html")
            return await bot.send_message(chat, text[:4000], buttons=buttons, parse_mode="html", link_preview=False)
        except Exception:
            log.exception("ticket relay to %s failed", chat)

    async def notify_admins(tid, header, body, msg):
        btn = [[Button.inline("✉️ پاسخ", f"tka:r:{tid}".encode()), Button.inline("🔒 بستن", f"tka:c:{tid}".encode())]]
        for a in ADMINS:
            await relay(a, header, body, msg, btn)

    # ------------------------------------------------------------ user callbacks
    @bot.on(events.CallbackQuery(pattern=rb"^tk:"))
    async def cb_user(e):
        uid = e.sender_id
        parts = e.data.decode().split(":")
        act = parts[1]
        if act == "my":
            pending.pop(uid, None)
            await e.answer()
            return await ctx.edit_view(e, *await user_list_view(uid))
        if act == "new":
            if await db.ticket_open_count(uid) >= db.TICKET_MAX_OPEN:
                return await e.answer(f"حداکثر {db.TICKET_MAX_OPEN} تیکت باز می‌تونی داشته باشی؛ اول یکی رو ببند.", alert=True)
            pending[uid] = ("tknew",)
            await e.answer()
            return await bot.send_message(uid, "🎫 **تیکت جدید**\nمشکل یا سوالت رو بنویس (متن، یا عکس/فایل همراه با توضیح).\n❌ لغو: /start")
        tid = int(parts[2])
        t = await db.ticket_get(tid)
        if not t or t["user_id"] != uid:
            return await e.answer("تیکت پیدا نشد", alert=True)
        if act == "v":
            await e.answer()
            return await ctx.edit_view(e, *await user_ticket_view(t))
        if t["status"] != "open":
            return await e.answer("این تیکت بسته شده؛ یه تیکت جدید بساز.", alert=True)
        if act == "r":
            pending[uid] = ("tkre", tid)
            await e.answer()
            return await bot.send_message(uid, f"✍️ پاسخت رو برای تیکت #{tid} بفرست (متن یا عکس/فایل).\n❌ لغو: /start")
        if act == "c":
            await db.ticket_set_status(tid, "closed")
            await e.answer("🔒 تیکت بسته شد")
            for a in ADMINS:
                try:
                    await bot.send_message(a, f"🔒 کاربر `{uid}` تیکت #{tid} رو بست.")
                except Exception:
                    pass
            return await ctx.edit_view(e, *await user_list_view(uid))

    # ------------------------------------------------------------ admin callbacks
    @bot.on(events.CallbackQuery(pattern=rb"^tka:", func=lambda e: e.sender_id in ADMINS))
    async def cb_admin(e):
        uid = e.sender_id
        parts = e.data.decode().split(":")
        act = parts[1]
        if act == "l":
            pending.pop(uid, None)
            await e.answer()
            return await ctx.edit_view(e, *await admin_list_view(int(parts[2])))
        tid = int(parts[2])
        t = await db.ticket_get(tid)
        if not t:
            return await e.answer("تیکت پیدا نشد", alert=True)
        if act == "v":
            await e.answer()
            return await ctx.edit_view(e, *await admin_ticket_view(t))
        if act == "r":
            if t["status"] != "open":
                return await e.answer("تیکت بسته‌ست؛ اول بازش کن.", alert=True)
            pending[uid] = ("tkad", tid)
            await e.answer()
            return await bot.send_message(uid, f"✉️ پاسخ تیکت #{tid} رو بفرست (متن یا عکس/فایل).\n❌ لغو: /start")
        if act == "c":
            owner = await db.ticket_set_status(tid, "closed")
            await e.answer("🔒 بسته شد")
            try:
                await bot.send_message(owner, f"🔒 تیکت #{tid} توسط پشتیبانی بسته شد. اگه مشکلت حل نشده تیکت جدید بساز 🙏")
            except Exception:
                pass
            return await ctx.edit_view(e, *await admin_ticket_view(await db.ticket_get(tid)))
        if act == "o":
            owner = await db.ticket_set_status(tid, "open")
            await e.answer("🔓 باز شد")
            try:
                await bot.send_message(owner, f"🔓 تیکت #{tid} دوباره باز شد.")
            except Exception:
                pass
            return await ctx.edit_view(e, *await admin_ticket_view(await db.ticket_get(tid)))

    # ------------------------------------------------------------ text/media input (called from main.on_pending)
    async def handle_pending(e, uid, kind, t):
        body = clip(t, MAX_LEN)
        has_media = bool(e.media and not getattr(e.media, "webpage", None))
        if not body and not has_media:
            return                                              # sticker/empty: ignore, keep waiting
        if not body:
            body = "[📎 فایل/عکس بدون توضیح]"
        k = kind[0]
        if k == "tknew":
            if await db.ticket_open_count(uid) >= db.TICKET_MAX_OPEN:
                pending.pop(uid, None)
                return await e.respond("⚠️ تیکت باز زیادی داری؛ اول یکی رو ببند.")
            subject = clip(body.splitlines()[0], 50)
            tid = await db.ticket_create(uid, subject, body, has_media)
            pending.pop(uid, None)
            who = await e.get_sender()
            name = utils.clean_name(getattr(who, "first_name", "") or "")
            await notify_admins(tid, f"🎫 <b>تیکت جدید #{tid}</b>\n👤 {esc(name)} (<code>{uid}</code>)", body, e.message)
            await e.respond(f"✅ تیکت **#{tid}** ثبت شد. پاسخ پشتیبانی همین‌جا برات میاد 🙏",
                            buttons=[[Button.inline("🎫 تیکت‌های من", b"tk:my")]])
            return
        tid = kind[1]
        t_ = await db.ticket_get(tid)
        if not t_ or t_["status"] != "open":
            pending.pop(uid, None)
            return await e.respond("⚠️ این تیکت بسته شده.")
        if k == "tkre":
            if t_["user_id"] != uid:
                pending.pop(uid, None)
                return
            if not await db.ticket_add_msg(tid, False, body, has_media):
                pending.pop(uid, None)
                return await e.respond("⚠️ این تیکت بسته شده.")
            pending.pop(uid, None)
            await notify_admins(tid, f"💬 <b>پیام جدید در تیکت #{tid}</b>\n👤 <code>{uid}</code>", body, e.message)
            return await e.respond("✅ پیامت ثبت شد.", buttons=[[Button.inline("🎫 تیکت‌های من", b"tk:my")]])
        if k == "tkad":
            if uid not in ADMINS:
                pending.pop(uid, None)
                return
            if not await db.ticket_add_msg(tid, True, body, has_media):
                pending.pop(uid, None)
                return await e.respond("⚠️ این تیکت بسته شده.")
            pending.pop(uid, None)
            btn = [[Button.inline("✍️ پاسخ", f"tk:r:{tid}".encode()), Button.inline("🔒 بستن", f"tk:c:{tid}".encode())]]
            await relay(t_["user_id"], f"📩 <b>پاسخ پشتیبانی — تیکت #{tid}</b>", body, e.message, btn)
            return await e.respond(f"✅ پاسخ برای کاربر `{t_['user_id']}` ارسال شد.",
                                   buttons=[[Button.inline("🎫 تیکت‌های باز", b"tka:l:0")]])

    return handle_pending, user_list_view, admin_list_view
