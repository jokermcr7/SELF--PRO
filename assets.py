"""Shared banner image (panel, welcome, games). Uploaded once, then reused by file reference."""
import os

_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
BANNER_PATH = next((os.path.join(_dir, n) for n in ("banner.jpg", "banner.png")
                    if os.path.exists(os.path.join(_dir, n))), None)

_cached = None          # Photo of the banner, for send_message/reply
PANEL_PHOTO = None      # InputPhoto of the banner, for inline results


async def banner():
    """Value for `file=`: the cached Telegram photo, or the local path the first time."""
    return _cached or BANNER_PATH


def remember(msg):
    """Call with the message returned by a send that used banner()."""
    global _cached
    try:
        if msg is not None and getattr(msg, "photo", None):
            _cached = msg.photo
    except Exception:
        pass
