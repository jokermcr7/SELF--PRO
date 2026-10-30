"""Live stream (RTMP) for groups / channels where the self owner is an admin.

`.لایو شروع [شبکه یا لینک]` creates a Telegram live (RTMP) in the current group/channel and relays a
satellite / international channel (default: Iran International) or any link into it with ffmpeg.

How it works (Telegram's own RTMP live):
  phone.createGroupCall(rtmp_stream=True)  ->  phone.getGroupCallStreamRtmpUrl()  ->  url + key
  ffmpeg  <source>  ->  flv  ->  url/key
The video is copied when it is already H.264 (very light on the server); otherwise (or when a quality is chosen)
it is re-encoded with libx264. The stream key is sent only to the owner's Saved Messages.
"""
import asyncio
import logging
import os
import random
import re
import shutil
import time

from telethon import errors, functions, types

import db
import utils

log = logging.getLogger("live")
Z = "‌"
LIVE_MAX = int(os.environ.get("LIVE_MAX", "5") or 5)          # how many lives may run on this server at once

# name -> (display name, YouTube search query, words that must appear in the channel name)
PRESETS = {
    "اینترنشنال": ("📡 ایران اینترنشنال", "Iran International live", ("iran international", "iranintl", "ایران اینترنشنال")),
    "بی_بی_سی": ("📡 بی‌بی‌سی فارسی", "BBC Persian live", ("bbc",)),
    "من_و_تو": ("📡 من و تو", "Manoto live", ("manoto", "من و تو")),
    "صدای_آمریکا": ("📡 صدای آمریکا (VOA فارسی)", "VOA Persian live", ("voa", "voice of america", "صدای آمریکا")),
    "یورونیوز": ("📡 یورونیوز فارسی", "Euronews Persian live", ("euronews",)),
}
ALIAS = {"ایران_اینترنشنال": "اینترنشنال", "iranintl": "اینترنشنال", "iran_international": "اینترنشنال", "ایران_اینترنشنال_تی_وی": "اینترنشنال",
         "bbc": "بی_بی_سی", "بی‌بی‌سی": "بی_بی_سی", "بیبیسی": "بی_بی_سی", "bbc_persian": "بی_بی_سی",
         "manoto": "من_و_تو", "منوتو": "من_و_تو", "من_وتو": "من_و_تو",
         "voa": "صدای_آمریکا", "صدای_امریکا": "صدای_آمریکا", "euronews": "یورونیوز"}
DEFAULT = "اینترنشنال"
QUALITY = {"اصلی": None, "کپی": None, "original": None, "copy": None,
           "360": (360, 700), "480": (480, 1200), "720": (720, 2500), "1080": (1080, 4500)}

_lives: dict = {}            # uid -> state dict


def _n(w):
    return re.sub(r"[\s‌]+", "_", (w or "").replace("ي", "ی").replace("ك", "ک").strip()).lower()


def _ffmpeg():
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:                                           # noqa: BLE001
        return None


def pick_preset(word):
    k = _n(word)
    k = ALIAS.get(k, k)
    return k if k in PRESETS else None


def is_direct(url):
    return bool(re.match(r"^(rtmps?|rtsp|udp|srt)://", url, re.I) or re.search(r"\.(m3u8|mpd|ts|mp4|flv)(\?|$)", url, re.I))


# ------------------------------------------------------------------ source resolving (blocking -> run in a thread)
def _ydl(opts=None):
    import yt_dlp
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True, "socket_timeout": 25,
         "format": "best[height<=1080]/best"}
    o.update(opts or {})
    return yt_dlp.YoutubeDL(o)


def _find_live_url(query, words):
    """Search YouTube for a *currently live* video whose channel name matches one of `words`."""
    with _ydl({"extract_flat": True}) as y:
        res = y.extract_info(f"ytsearch12:{query}", download=False)
    cands = []
    for it in (res or {}).get("entries") or []:
        ch = ((it.get("channel") or it.get("uploader") or "") + " " + (it.get("title") or "")).lower()
        if any(w in ch for w in words):
            cands.append(it)
    cands.sort(key=lambda i: 0 if i.get("live_status") == "is_live" else 1)
    for it in cands[:4]:
        url = it.get("url") or f"https://www.youtube.com/watch?v={it.get('id')}"
        if it.get("live_status") in ("is_live", None):
            return url
    return None


def resolve_source(src):
    """-> (media url, headers dict, title). `src` is a preset key or a link."""
    title = ""
    if src in PRESETS:
        name, query, words = PRESETS[src]
        page = _find_live_url(query, words)
        if not page:
            raise RuntimeError("الان لایو زنده‌ای از این شبکه پیدا نشد")
        title = name
    else:
        page = src
        if is_direct(page):
            return page, {}, src[:60]
    with _ydl() as y:
        info = y.extract_info(page, download=False)
    if info.get("_type") == "playlist":
        info = (info.get("entries") or [None])[0] or {}
    url = info.get("url")
    if not url and info.get("requested_formats"):
        url = info["requested_formats"][0].get("url")
    if not url:
        raise RuntimeError("لینک ویدیو از این منبع در نیومد")
    return url, info.get("http_headers") or {}, title or (info.get("title") or "")[:60]


# ------------------------------------------------------------------ ffmpeg
def build_cmd(ff, media, headers, rtmp, quality, force_encode=False):
    cmd = [ff, "-hide_banner", "-loglevel", "warning", "-nostats"]
    if re.match(r"^https?://", media, re.I):
        hdr = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items() if k.lower() in ("user-agent", "referer", "cookie", "origin"))
        if hdr:
            cmd += ["-headers", hdr]
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    cmd += ["-i", media, "-map", "0:v:0?", "-map", "0:a:0?"]
    q = QUALITY.get(quality) if quality else None
    if q or force_encode:
        h, kbps = q or (480, 1200)
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-profile:v", "main", "-pix_fmt", "yuv420p",
                "-vf", f"scale=-2:{h}", "-r", "25", "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
                "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.2)}k", "-bufsize", f"{kbps * 2}k"]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", "-f", "flv", rtmp]
    return cmd


async def _supervisor(c, uid, st):
    """Keeps ffmpeg alive: re-resolves the source on every restart (links expire), falls back to re-encoding."""
    ff = _ffmpeg()
    fails = 0
    force = False
    try:
        while not st["stop"]:
            try:
                media, headers, title = await asyncio.to_thread(resolve_source, st["src"])
                st["title"] = title or st["title"]
            except Exception as ex:                                  # noqa: BLE001
                fails += 1
                st["error"] = f"منبع: {str(ex)[:100]}"
                if fails >= 6:
                    break
                await asyncio.sleep(10)
                continue
            cmd = build_cmd(ff, media, headers, st["rtmp"], st["quality"], force)
            t0 = time.time()
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            st["proc"] = proc
            st["encode"] = force or bool(QUALITY.get(st["quality"] or ""))
            err = b""
            try:
                err = await proc.stderr.read()                       # returns when ffmpeg exits
            except Exception:                                        # noqa: BLE001
                pass
            await proc.wait()
            if st["stop"]:
                break
            ran = time.time() - t0
            st["error"] = err.decode("utf-8", "ignore")[-160:].strip().replace("\n", " ")
            if ran < 30:
                fails += 1
                if not force and not st["encode"]:
                    force = True                                      # copy failed fast -> probably not H.264: re-encode
            else:
                fails = 0
            if fails >= 6:
                break
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        raise
    finally:
        p = st.get("proc")
        if p and p.returncode is None:
            try:
                p.kill()
            except ProcessLookupError:
                pass
    if not st["stop"]:                                                # gave up by itself
        st["dead"] = True
        try:
            await c.send_message("me", f"⚠️ لایوی {st['chat_title']} قطع شد و بعد از چند تلاش وصل نشد.\n{st.get('error', '')}")
        except Exception:                                            # noqa: BLE001
            pass


# ------------------------------------------------------------------ Telegram side
def _call_of(res):
    for u in getattr(res, "updates", None) or []:
        call = getattr(u, "call", None)
        if call is not None and hasattr(call, "access_hash"):
            return types.InputGroupCall(id=call.id, access_hash=call.access_hash)
    return None


async def _rtmp(c, peer):
    try:
        r = await c(functions.phone.GetGroupCallStreamRtmpUrlRequest(peer=peer, revoke=False))
    except TypeError:
        r = await c(functions.phone.GetGroupCallStreamRtmpUrlRequest(peer=peer))
    url = r.url if r.url.endswith("/") else r.url + "/"
    return url + r.key, r.url, r.key


def _fmt_dur(s):
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def help_text():
    names = "\n".join(f"• `{k}` ← {v[0]}" for k, v in PRESETS.items())
    return ("📡 **لایو ماهواره‌ای**\n"
            "توی گروه یا کانالی که **ادمینی** لایو می‌سازه و تصویر شبکه رو بهش وصل می‌کنه.\n\n"
            "`.لایو شروع` ← پیش‌فرض: ایران اینترنشنال\n"
            "`.لایو شروع بی_بی_سی` | `.لایو شروع https://...` (لینک یوتیوب، m3u8، ...)\n"
            "`.لایو منبع من_و_تو` ← عوض کردن شبکه وقتی لایو روشنه\n"
            "`.لایو کیفیت 720` (۳۶۰ | ۴۸۰ | ۷۲۰ | ۱۰۸۰ | اصلی)\n"
            "`.لایو وضعیت` | `.لایو کلید` (آدرس و کلید RTMP توی پیام‌های ذخیره‌شده) | `.لایو توقف`\n\n"
            f"شبکه‌ها:\n{names}\n\n"
            "⚠️ برای کانال/ابرگروه لازمه؛ گروه معمولی اول باید به ابرگروه تبدیل بشه. پخش‌کردن محتوای شبکه‌ها مسئولیتش با خودته.")


async def c_live(c, e, uid, arg):
    a = (arg or "").strip()
    parts = a.split(None, 1)
    sub = _n(parts[0]) if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    if sub in ("", "راهنما", "help"):
        return await e.edit(help_text())
    if sub in ("شروع", "start", "روشن", "on"):
        return await _start(c, e, uid, rest)
    if sub in ("توقف", "stop", "خاموش", "off", "پایان"):
        return await _stop_cmd(c, e, uid)
    if sub in ("وضعیت", "status"):
        return await _status(e, uid)
    if sub in ("کلید", "key", "rtmp"):
        st = _lives.get(uid)
        if not st:
            return await e.edit("لایوی روشن نیست")
        await c.send_message("me", f"🔑 **RTMP لایوی {st['chat_title']}**\n`{st['url']}`\nکلید: `{st['key']}`\n(برای OBS؛ جایی پخش نکن)")
        return await e.edit("🔑 آدرس و کلید رو توی پیام‌های ذخیره‌شده‌ات فرستادم")
    if sub in ("کیفیت", "quality"):
        q = _n(utils.to_en_digits(rest))
        if q not in QUALITY:
            return await e.edit("کیفیت: `360` | `480` | `720` | `1080` | `اصلی`")
        await db.uset(uid, "live_q", "" if QUALITY[q] is None else q)
        st = _lives.get(uid)
        if st:
            st["quality"] = "" if QUALITY[q] is None else q
            await _restart_ff(st)
        return await e.edit(f"🎞 کیفیت لایو: **{rest}**" + (" (اعمال شد)" if st else ""))
    if sub in ("منبع", "source", "شبکه"):
        st = _lives.get(uid)
        if not st:
            return await e.edit("لایوی روشن نیست؛ اول `.لایو شروع`")
        src = pick_preset(rest) or (rest if rest.startswith(("http", "rtmp", "rtsp", "udp", "srt")) else None)
        if not src:
            return await e.edit("شبکه یا لینک معتبر بده؛ `.لایو` رو بزن تا لیست رو ببینی")
        st["src"] = src
        await _restart_ff(st)
        return await e.edit(f"🔁 منبع عوض شد: **{PRESETS[src][0] if src in PRESETS else src[:50]}**")
    # `.لایو اینترنشنال` etc. = shortcut for start
    if pick_preset(a) or a.startswith("http"):
        return await _start(c, e, uid, a)
    await e.edit(help_text())


async def _restart_ff(st):
    p = st.get("proc")
    if p and p.returncode is None:
        try:
            p.terminate()
        except ProcessLookupError:
            pass


async def _start(c, e, uid, rest):
    if e.is_private:
        return await e.edit("📡 لایو رو توی یه گروه یا کانال بزن (جایی که ادمینی)")
    if uid in _lives and not _lives[uid].get("dead"):
        return await e.edit(f"یه لایو روشن داری ({_lives[uid]['chat_title']}). اول `.لایو توقف`")
    if len([s for s in _lives.values() if not s.get("dead")]) >= LIVE_MAX and uid not in db.ADMINS:
        return await e.edit("⏳ ظرفیت لایوهای سرور پره؛ چند دقیقه‌ی دیگه امتحان کن")
    if not _ffmpeg():
        return await e.edit("⚠️ ffmpeg روی سرور نیست")
    src = DEFAULT
    if rest:
        src = pick_preset(rest) or (rest if rest.startswith(("http", "rtmp", "rtsp", "udp", "srt")) else None)
        if not src:
            return await e.edit("شبکه یا لینک معتبر بده؛ `.لایو` رو بزن تا لیست رو ببینی")
    ent = await e.get_chat()
    if isinstance(ent, types.Chat):
        return await e.edit("⚠️ این یه گروه معمولیه؛ لایو RTMP فقط برای ابرگروه و کانال کار می‌کنه (گروه رو به ابرگروه تبدیل کن)")
    await e.edit("🔎 در حال پیدا کردن منبع و ساخت لایو...")
    try:                                                          # fail early when the source is not available
        media, _h, title = await asyncio.to_thread(resolve_source, src)
    except Exception as ex:                                      # noqa: BLE001
        return await e.edit(f"⚠️ منبع در دسترس نیست: {str(ex)[:120]}")
    peer = await e.get_input_chat()
    chat_title = getattr(ent, "title", None) or str(e.chat_id)
    try:
        res = await c(functions.phone.CreateGroupCallRequest(peer=peer, random_id=random.getrandbits(31), rtmp_stream=True,
                                                             title=f"{title or 'Live'}"[:60]))
        call = _call_of(res)
        full, url, key = await _rtmp(c, peer)
    except errors.ChatAdminRequiredError:
        return await e.edit("⛔ برای ساخت لایو باید ادمین با دسترسی «مدیریت چت‌های ویدیویی» باشی")
    except errors.RPCError as ex:
        msg = type(ex).__name__
        if "GROUPCALL" in msg.upper() or "ALREADY" in msg.upper():
            return await e.edit("⚠️ یه چت صوتی/ویدیویی یا لایو دیگه توی این چت روشنه؛ اول تمومش کن")
        return await e.edit(f"⚠️ ساخت لایو ناموفق بود ({msg})")
    st = {"chat_id": e.chat_id, "chat_title": chat_title, "call": call, "peer": peer, "src": src, "rtmp": full, "url": url, "key": key,
          "quality": await db.uget(uid, "live_q", "") or "", "started": time.time(), "stop": False, "title": title, "error": "", "proc": None}
    _lives[uid] = st
    st["task"] = asyncio.create_task(_supervisor(c, uid, st))
    await asyncio.sleep(6)
    ok = st.get("proc") is not None and st["proc"].returncode is None
    await c.send_message("me", f"🔑 **RTMP لایوی {chat_title}** (برای OBS)\n`{url}`\nکلید: `{key}`")
    await e.edit(("🔴 **لایو شروع شد!**" if ok else "🟡 **لایو ساخته شد؛ منبع داره وصل میشه...**")
                 + f"\n📺 {title or src[:40]}\n💬 {chat_title}\n"
                 "توی بالای گروه/کانال لایو رو می‌بینی. `.لایو وضعیت` | `.لایو توقف`")


async def _status(e, uid):
    st = _lives.get(uid)
    if not st:
        return await e.edit("⚪ لایوی روشن نیست")
    p = st.get("proc")
    alive = p is not None and p.returncode is None
    await e.edit(f"{'🔴 زنده' if alive and not st.get('dead') else '🟡 در حال اتصال/قطع'}\n💬 {st['chat_title']}\n📺 {st.get('title') or st['src'][:40]}\n"
                 f"⏱ {_fmt_dur(time.time() - st['started'])}\n🎞 {'بازکدگذاری ' + (st['quality'] or '480') + 'p' if st.get('encode') else 'کپی مستقیم (بدون افت کیفیت)'}"
                 + (f"\n⚠️ {st['error']}" if st.get("error") and not alive else ""))


async def _stop_cmd(c, e, uid):
    if uid not in _lives:
        return await e.edit("⚪ لایوی روشن نیست")
    await stop(c, uid)
    await e.edit("⏹ لایو تموم شد")


async def stop(c, uid):
    st = _lives.pop(uid, None)
    if not st:
        return
    st["stop"] = True
    p = st.get("proc")
    if p and p.returncode is None:
        try:
            p.terminate()
        except ProcessLookupError:
            pass
    t = st.get("task")
    if t:
        t.cancel()
    if st.get("call") is not None and c is not None:
        try:
            await c(functions.phone.DiscardGroupCallRequest(call=st["call"]))
        except Exception as ex:                                      # noqa: BLE001
            log.warning("discard call failed: %s", ex)


async def stop_all(uid, c=None):
    """Called when the self stops / diamonds run out."""
    await stop(c, uid)


COMMAND_NAMES = {"لایو": ("live", c_live), "پخش_زنده": ("live", c_live)}


def install(COMMANDS, FA):
    for name, (key, fn) in COMMAND_NAMES.items():
        COMMANDS[key] = fn
        FA[name] = key
