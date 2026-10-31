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
import collections
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


def ffmpeg_kind():
    """'system' (apt/nix ffmpeg, dynamic) or 'static' (imageio's bundled build - can segfault on DNS in some containers)."""
    return "system" if shutil.which("ffmpeg") else "static"


def pick_preset(word):
    k = _n(word)
    k = ALIAS.get(k, k)
    return k if k in PRESETS else None


def is_direct(url):
    return bool(re.match(r"^(rtmps?|rtsp|udp|srt)://", url, re.I) or re.search(r"\.(m3u8|mpd|ts|mp4|flv)(\?|$)", url, re.I))


# ------------------------------------------------------------------ source resolving (blocking -> run in a thread)
_COOKIE_FILE = None


def _cookie_file():
    """Optional: YT_COOKIES (Netscape cookies.txt content) lets yt-dlp pass YouTube's bot check on datacenter IPs."""
    global _COOKIE_FILE
    txt = os.environ.get("YT_COOKIES", "")
    if not txt:
        return None
    if _COOKIE_FILE is None:
        import tempfile
        fd, p = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write(txt.replace("\\n", "\n"))
        _COOKIE_FILE = p
    return _COOKIE_FILE


def _ydl(opts=None):
    import yt_dlp
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True, "socket_timeout": 25,
         "ignore_no_formats_error": True, "check_formats": False}
    if os.environ.get("LIVE_PROXY"):
        o["proxy"] = os.environ["LIVE_PROXY"]
    ck = _cookie_file()
    if ck:
        o["cookiefile"] = ck
    o.update(opts or {})
    return yt_dlp.YoutubeDL(o)


# YouTube clients that return HLS manifests for live streams without needing JS challenges, tried in order
_CLIENTS = (["web_safari"], ["tv"], ["android_vr"], ["mweb"], ["ios"], None)


def _find_live_url(query, words):
    """Search YouTube with the *Live* filter for a currently live video whose channel name matches `words`."""
    from urllib.parse import quote_plus
    urls = (f"https://www.youtube.com/results?search_query={quote_plus(query)}&sp=EgJAAQ%253D%253D",   # filter: Live
            f"ytsearch12:{query}")
    for u in urls:
        try:
            with _ydl({"extract_flat": True, "playlistend": 15}) as y:
                res = y.extract_info(u, download=False)
        except Exception as ex:                                  # noqa: BLE001
            log.warning("live search failed (%s): %s", u[:30], str(ex)[:120])
            continue
        cands = []
        for it in (res or {}).get("entries") or []:
            ch = ((it.get("channel") or it.get("uploader") or "") + " " + (it.get("title") or "")).lower()
            if any(w in ch for w in words):
                cands.append(it)
        cands.sort(key=lambda i: 0 if i.get("live_status") == "is_live" else 1)
        for it in cands[:3]:
            if it.get("live_status") in ("is_live", None):
                return it.get("url") or f"https://www.youtube.com/watch?v={it.get('id')}"
    return None


def pick_media(info):
    """Choose what ffmpeg should read from a yt-dlp info dict -> (media, headers). media = url or [video_url, audio_url]."""
    fm = [f for f in (info.get("formats") or []) if f.get("url")]
    hdr = info.get("http_headers") or {}

    def h(f):
        return f.get("height") or 0

    def has(f, k):
        return (f.get(k) or "none") != "none"
    muxed = [f for f in fm if has(f, "vcodec") and has(f, "acodec") and h(f) <= 1080]
    hls = [f for f in muxed if "m3u8" in (f.get("protocol") or "")]
    for pool in (hls, muxed):                                    # live HLS first, then any muxed file
        if pool:
            best = max(pool, key=h)
            return best["url"], best.get("http_headers") or hdr
    vid = [f for f in fm if has(f, "vcodec") and not has(f, "acodec") and h(f) <= 1080]
    aud = [f for f in fm if has(f, "acodec") and not has(f, "vcodec")]
    if vid and aud:                                              # separate tracks: ffmpeg muxes them
        return [max(vid, key=h)["url"], max(aud, key=lambda f: f.get("abr") or 0)["url"]], hdr
    if info.get("manifest_url"):
        return info["manifest_url"], hdr
    if info.get("url"):
        return info["url"], hdr
    return None, hdr


def _probe(media, headers):
    """Download the first bytes of the real media (HLS: newest segment) with the same headers ffmpeg will use.
    Raises on 403/404 so a client whose links are blocked (PO token / IP) is skipped before ffmpeg starts."""
    import urllib.request
    from urllib.parse import urljoin
    hd = {k: v for k, v in (headers or {}).items() if k.lower() in ("user-agent", "referer", "origin", "cookie", "accept-language")}
    hd.setdefault("User-Agent", "Mozilla/5.0")

    def get(u, n=400000):
        with urllib.request.urlopen(urllib.request.Request(u, headers=hd), timeout=15) as r:
            return r.read(n)
    for m in (media if isinstance(media, (list, tuple)) else [media]):
        if not m.startswith("http"):
            continue
        if ".m3u8" not in m and "manifest" not in m:
            get(m, 2048)
            continue
        txt = get(m).decode("utf-8", "ignore")
        for _ in range(2):                                       # master -> media playlist
            if "#EXT-X-STREAM-INF" not in txt:
                break
            lines = txt.splitlines()
            var = None
            for i, ln in enumerate(lines):
                if ln.startswith("#EXT-X-STREAM-INF"):
                    var = next((x for x in lines[i + 1:] if x and not x.startswith("#")), None)
                    break
            if not var:
                break
            m = urljoin(m, var)
            txt = get(m).decode("utf-8", "ignore")
        segs = [x for x in txt.splitlines() if x and not x.startswith("#")]
        if not segs:
            raise RuntimeError("playlist بدون سگمنت")
        get(urljoin(m, segs[-1]), 2048)


def _extract(page):
    errs = []
    for strict in (True, False):                                 # strict = real YouTube error text; then the tolerant pass
        for cl in _CLIENTS:
            opts = {"extractor_args": {"youtube": {"player_client": cl}}} if cl else {}
            if strict:
                opts["ignore_no_formats_error"] = False
            try:
                with _ydl(opts) as y:
                    info = y.extract_info(page, download=False)
                if info and info.get("_type") == "playlist":
                    info = (info.get("entries") or [None])[0] or {}
                media, hdr = pick_media(info or {})
                if media:
                    try:
                        _probe(media, hdr)
                    except Exception as pe:                      # noqa: BLE001
                        errs.append(f"{(cl or ['default'])[0]}: لینک بلاک ({str(pe)[:60]})")
                        continue
                    return media, hdr, (info.get("title") or "")
                errs.append(f"{(cl or ['default'])[0]}: بدون فرمت")
            except Exception as ex:                              # noqa: BLE001
                m = re.sub(r"\x1b\[[0-9;]*m|ERROR:\s*|\[youtube\]\s*[\w-]+:\s*", "", str(ex)).strip()
                errs.append(f"{(cl or ['default'])[0]}: {m[:90]}")
        if strict and errs:
            log.warning("live source strict pass failed: %s", " || ".join(errs)[:600])
    uniq = []
    for x in errs:
        k = x.split(": ", 1)[-1]
        if k not in [u.split(": ", 1)[-1] for u in uniq]:
            uniq.append(x)
    raise RuntimeError(" | ".join(uniq[:3])[:300] or "هیچ فرمت قابل پخشی پیدا نشد")


def resolve_source(src):
    """-> (media url or [video, audio], headers dict, title). `src` is a preset key or a link."""
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
    media, hdr, t = _extract(page)
    return media, hdr, title or t[:60]


# ------------------------------------------------------------------ ffmpeg
def build_cmd(ff, media, headers, rtmp, quality, force_encode=False):
    cmd = [ff, "-hide_banner", "-nostdin", "-loglevel", "warning", "-nostats", "-progress", "pipe:1", "-stats_period", "2"]
    medias = media if isinstance(media, (list, tuple)) else [media]
    for m in medias:
        if re.match(r"^https?://", m, re.I):
            hdr = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items() if k.lower() in ("user-agent", "referer", "cookie", "origin"))
            if hdr:
                cmd += ["-headers", hdr]
            cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        cmd += ([] if re.search(r"m3u8|manifest|^rtmps?://", m, re.I) else ["-re"]) + ["-i", m]
    cmd += ["-map", "0:v:0?", "-map", "1:a:0?" if len(medias) > 1 else "0:a:0?"]
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
            proc = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            st["proc"] = proc
            st["encode"] = force or bool(QUALITY.get(st["quality"] or ""))
            st["log"].clear()

            async def read_err():
                async for line in proc.stderr:
                    t = line.decode("utf-8", "ignore").strip()
                    if t:
                        st["log"].append(t[:200])
                        log.warning("ffmpeg[%s]: %s", uid, t[:200])

            async def read_prog():
                async for line in proc.stdout:
                    t = line.decode("utf-8", "ignore").strip()
                    if t.startswith(("total_size=", "out_time=", "speed=", "bitrate=")):
                        k, _, v = t.partition("=")
                        st["prog"][k] = v

            async def watchdog():
                last, since = -1, time.time()
                while proc.returncode is None:
                    await asyncio.sleep(5)
                    cur = _sent_bytes(st)
                    if cur != last:
                        last, since = cur, time.time()
                    elif time.time() - since > 45:               # nothing sent for 45 s (403 / dead link) -> restart with a fresh link
                        st["log"].append("بدون خروجی ۴۵ ثانیه؛ ریستارت با لینک تازه")
                        try:
                            proc.terminate()
                        except ProcessLookupError:
                            pass
                        return

            rt = [asyncio.create_task(read_err()), asyncio.create_task(read_prog()), asyncio.create_task(watchdog())]
            await proc.wait()
            await asyncio.gather(*rt, return_exceptions=True)
            err = " | ".join(st["log"]).encode()
            if st["stop"]:
                break
            ran = time.time() - t0
            st["error"] = err.decode("utf-8", "ignore")[-300:].strip()
            if proc.returncode == -11:
                fails += 3                                              # segfault: retrying will not help
            if ran < 30:
                fails += 1
                txt = " ".join(st["log"])
                if (not force and not st["encode"] and proc.returncode != -11
                        and re.search(r"codec|not supported|unsupported|header|invalid data|flv", txt, re.I)
                        and not re.search(r"403|forbidden|server returned|404", txt, re.I)):
                    force = True                                      # copy failed because of the codec -> re-encode
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


def _sent_bytes(st):
    v = (st.get("prog") or {}).get("total_size", "0")
    return int(v) if str(v).isdigit() else 0


def _diag(st):
    p = st.get("proc")
    alive = p is not None and p.returncode is None
    pr = st.get("prog") or {}
    line = (f"ffmpeg[{ffmpeg_kind()}] {'روشن' if alive else 'خاموش (کد ' + str(getattr(p, 'returncode', '?')) + ')'}"
            f" | ارسال‌شده: {_sent_bytes(st) // 1024} KB | سرعت: {pr.get('speed', '-')}")
    if getattr(p, "returncode", None) == -11:
        line += ("\n💥 ffmpeg کرش کرد (SIGSEGV)؛ معمولاً یعنی ffmpegِ استاتیکِ همراه برنامه روی این سرور کار نمی‌کنه. "
                 "ffmpeg سیستمی نصب کن: توی Variables ریلوی `RAILPACK_DEPLOY_APT_PACKAGES=ffmpeg` و `NIXPACKS_APT_PKGS=ffmpeg` رو بذار و Redeploy بزن.")
    last = list(st.get("log") or [])[-3:]
    if last:
        line += "\nخطای ffmpeg: " + " | ".join(last)
    elif st.get("error"):
        line += "\n" + st["error"][:200]
    return line


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
    if ffmpeg_kind() == "static":
        log.warning("live: using the bundled static ffmpeg (system ffmpeg recommended)")
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
        m = str(ex)
        hint = ("\n(یوتیوب IP سرور رو به‌عنوان ربات می‌شناسه؛ متغیر YT_COOKIES یا LIVE_PROXY رو تنظیم کن یا لینک مستقیم m3u8 بده)"
                if re.search(r"sign in|not a bot|429|403", m, re.I) else "")
        return await e.edit(f"⚠️ منبع در دسترس نیست: {m[:320]}{hint}")
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
          "quality": await db.uget(uid, "live_q", "") or "", "started": time.time(), "stop": False, "title": title, "error": "", "proc": None,
          "log": collections.deque(maxlen=8), "prog": {}}
    _lives[uid] = st
    st["task"] = asyncio.create_task(_supervisor(c, uid, st))
    await c.send_message("me", f"🔑 **RTMP لایوی {chat_title}** (برای OBS)\n`{url}`\nکلید: `{key}`")
    sent = False
    for _ in range(20):                                           # up to ~20 s: wait until bytes really flow to Telegram
        await asyncio.sleep(1)
        p = st.get("proc")
        if p is not None and p.returncode is None and _sent_bytes(st) > 30000:
            sent = True
            break
    head = "🔴 **لایو شروع شد و داره پخش میشه!**" if sent else "🟡 **لایو ساخته شد ولی هنوز تصویری به تلگرام نرسیده**"
    tail = "" if sent else ("\n" + _diag(st) + "\nچند ثانیه صبر کن؛ اگه نیومد `.لایو وضعیت` بزن و متنش رو بفرست.")
    await e.edit(f"{head}\n📺 {title or src[:40]}\n💬 {chat_title}{tail}\n`.لایو وضعیت` | `.لایو توقف`")


async def _status(e, uid):
    st = _lives.get(uid)
    if not st:
        return await e.edit("⚪ لایوی روشن نیست")
    p = st.get("proc")
    alive = p is not None and p.returncode is None
    await e.edit(f"{'🔴 زنده' if alive and not st.get('dead') else '🟡 در حال اتصال/قطع'}\n💬 {st['chat_title']}\n📺 {st.get('title') or st['src'][:40]}\n"
                 f"⏱ {_fmt_dur(time.time() - st['started'])}\n🎞 {'بازکدگذاری ' + (st['quality'] or '480') + 'p' if st.get('encode') else 'کپی مستقیم (بدون افت کیفیت)'}"
                 + f"\n📤 {_diag(st)}")


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
