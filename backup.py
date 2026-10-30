"""Automatic database backup for the owner(s).

* Pure Python logical dump (no `pg_dump` needed on Railway): every table of the `public` schema -> one
  gzip-compressed JSON file `xtm-backup-YYYYmmdd-HHMM.json.gz`.
* Sent to every ADMIN_IDS account in the bot chat every night (default 03:00 Asia/Tehran) and on demand
  (menu -> «💾 بکاپ دیتابیس» or `/backup`). A missed night (restart / deploy) is caught up automatically.
* Sessions inside the backup are already encrypted with ENCRYPT_KEY: keep that key safe, without it the saved
  sessions cannot be read. The file is still sensitive (wallets, phones) -> keep it private.
* Restore (replaces ALL data in the database with the backup; stop the bot first):
      DATABASE_URL=... python backup.py restore xtm-backup-20261030-0300.json.gz
  A backup that was split in parts is joined first:  cat xtm-backup-*.part* > xtm-backup.json.gz

Env (all optional): BACKUP_ENABLED=1  BACKUP_HOUR=3  BACKUP_MINUTE=0
"""
import asyncio
import base64
import datetime as dt
import decimal
import gzip
import io
import json
import logging
import os
import sys
import uuid
from zoneinfo import ZoneInfo

import db

log = logging.getLogger("backup")
TZ = ZoneInfo("Asia/Tehran")
ENABLED = os.getenv("BACKUP_ENABLED", "1") != "0"
HOUR = min(max(int(os.getenv("BACKUP_HOUR", "3")), 0), 23)
MINUTE = min(max(int(os.getenv("BACKUP_MINUTE", "0")), 0), 59)
MAX_PART = 45 * 1024 * 1024          # Telegram upload comfort limit per file
FORMAT = 1


# ------------------------------------------------------------------ (de)serialisation
def _enc(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, dt.datetime):
        return {"$t": "dt", "v": v.isoformat()}
    if isinstance(v, dt.date):
        return {"$t": "d", "v": v.isoformat()}
    if isinstance(v, dt.time):
        return {"$t": "t", "v": v.isoformat()}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"$t": "b", "v": base64.b64encode(bytes(v)).decode()}
    if isinstance(v, decimal.Decimal):
        return {"$t": "dec", "v": str(v)}
    if isinstance(v, uuid.UUID):
        return {"$t": "u", "v": str(v)}
    if isinstance(v, (list, tuple)):
        return [_enc(x) for x in v]
    return str(v)


def _dec(v):
    if isinstance(v, list):
        return [_dec(x) for x in v]
    if isinstance(v, dict) and "$t" in v:
        t, x = v["$t"], v["v"]
        return {"dt": dt.datetime.fromisoformat, "d": dt.date.fromisoformat, "t": dt.time.fromisoformat,
                "b": base64.b64decode, "dec": decimal.Decimal, "u": uuid.UUID}[t](x)
    return v


def _pack(tables: dict) -> bytes:
    doc = {"format": FORMAT, "created": dt.datetime.now(TZ).isoformat(), "tables": tables}
    return gzip.compress(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode(), 6)


# ------------------------------------------------------------------ dump
async def dump() -> tuple[bytes, dict]:
    """Consistent snapshot of the whole database. Returns (gzip bytes, {table: row_count})."""
    tables, counts = {}, {}
    async with db.pool.acquire() as c:
        async with c.transaction(isolation="repeatable_read", readonly=True):
            names = [r["tablename"] for r in await c.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1")]
            for t in names:
                cols = [r["column_name"] for r in await c.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=$1 ORDER BY ordinal_position", t)]
                rows = await c.fetch(f'SELECT * FROM "{t}"')
                tables[t] = {"columns": cols, "rows": [[_enc(r[k]) for k in cols] for r in rows]}
                counts[t] = len(rows)
    data = await asyncio.to_thread(_pack, tables)
    return data, counts


# ------------------------------------------------------------------ restore (command line)
async def restore(path: str):
    with open(path, "rb") as f:
        doc = json.loads(gzip.decompress(f.read()).decode())
    if doc.get("format") != FORMAT:
        raise SystemExit("unknown backup format")
    await db.init()                                   # creates any missing table / column first
    async with db.pool.acquire() as c:
        async with c.transaction():
            have = {r["tablename"] for r in await c.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
            for t, d in doc["tables"].items():
                if t not in have:
                    print(f"skip {t} (table does not exist)")
                    continue
                await c.execute(f'TRUNCATE "{t}"')
                cols = d["columns"]
                if d["rows"]:
                    q = (f'INSERT INTO "{t}"(' + ",".join(f'"{k}"' for k in cols) + ") VALUES(" +
                         ",".join(f"${i + 1}" for i in range(len(cols))) + ")")
                    await c.executemany(q, [[_dec(v) for v in row] for row in d["rows"]])
                if "id" in cols:                      # keep SERIAL counters ahead of the restored ids
                    await c.execute(
                        f"SELECT setval(pg_get_serial_sequence('\"{t}\"','id'), COALESCE(max(id),1), max(id) IS NOT NULL) "
                        f'FROM "{t}" WHERE pg_get_serial_sequence(\'"{t}"\',\'id\') IS NOT NULL')
                print(f"restored {t}: {len(d['rows'])} rows")
    print("done. start the bot again.")


# ------------------------------------------------------------------ send to the owner(s)
def _size(n: int) -> str:
    return f"{n / 1024 / 1024:.2f} MB" if n >= 1024 * 1024 else f"{n / 1024:.1f} KB"


async def run_backup(bot, admins, reason="auto"):
    """Build the backup and send it to every admin. Returns (ok_count, size, counts)."""
    data, counts = await dump()
    now = dt.datetime.now(TZ)
    name = f"xtm-backup-{now:%Y%m%d-%H%M}.json.gz"
    parts = [data[i:i + MAX_PART] for i in range(0, len(data), MAX_PART)] or [data]
    total_rows = sum(counts.values())
    tbl = " | ".join(f"{k}: {v:,}" for k, v in counts.items())
    ok = 0
    for a in admins:
        try:
            for i, part in enumerate(parts, 1):
                bio = io.BytesIO(part)
                bio.name = name if len(parts) == 1 else f"{name}.part{i}of{len(parts)}"
                cap = (f"💾 بکاپ دیتابیس ({'دستی' if reason == 'manual' else 'خودکار شبانه'})\n"
                       f"🕒 {now:%Y-%m-%d %H:%M} | 📦 {_size(len(data))} | 🧾 {total_rows:,} ردیف\n"
                       + (f"📎 بخش {i} از {len(parts)} (قبل از بازیابی با `cat` بهم بچسبون)\n" if len(parts) > 1 else "")
                       + f"{tbl}\n\n⚠️ شامل شماره و نشست‌های رمزنگاری‌شده است؛ جایی منتشرش نکن و ENCRYPT_KEY رو نگه دار.\n"
                         "بازیابی: `python backup.py restore فایل`")
                await bot.send_file(a, bio, caption=cap[:1000], force_document=True)
            ok += 1
        except Exception:
            log.exception("sending backup to %s failed", a)
    if ok:
        await db.set_("backup_last", f"{now:%Y-%m-%d %H:%M}|{len(data)}|{total_rows}")
    return ok, len(data), counts


async def enabled() -> bool:
    return (await db.get("backup_on", "1" if ENABLED else "0")) == "1"


async def toggle() -> bool:
    new = not await enabled()
    await db.set_("backup_on", "1" if new else "0")
    return new


async def status_text() -> str:
    last = await db.get("backup_last", "")
    if last:
        when, size, rows = last.split("|")
        last_t = f"{when} ({_size(int(size))}، {int(rows):,} ردیف)"
    else:
        last_t = "هنوز بکاپی گرفته نشده"
    return (f"<blockquote>💾 بکاپ دیتابیس | فقط مدیر</blockquote>\n"
            f"وضعیت بکاپ شبانه: {'🟢 روشن' if await enabled() else '🔴 خاموش'}\n"
            f"⏰ زمان: هر شب {HOUR:02d}:{MINUTE:02d} (به وقت تهران)\n"
            f"🕒 آخرین بکاپ: {last_t}\n\n"
            "فایل بکاپ توی همین چت برای همه‌ی مدیرها فرستاده میشه. «بکاپ همین الان» رو برای گرفتن فوری بزن.")


# ------------------------------------------------------------------ nightly loop
async def loop(bot, admins):
    fails = 0
    while True:
        try:
            await asyncio.sleep(45)
            if not await enabled():
                continue
            now = dt.datetime.now(TZ)
            if now.hour * 60 + now.minute < HOUR * 60 + MINUTE:
                continue
            today = now.strftime("%Y-%m-%d")
            # atomic "first one today wins" (safe even if two processes overlap during a deploy)
            won = await db.pool.fetchval(
                "INSERT INTO settings(key,value) VALUES('backup_day',$1) "
                "ON CONFLICT(key) DO UPDATE SET value=$1 WHERE settings.value<>$1 RETURNING 1", today)
            if not won:
                continue
            ok, size, _ = await run_backup(bot, admins, "auto")
            if not ok:
                raise RuntimeError("no admin received the backup")
            fails = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("nightly backup failed")
            fails += 1
            if fails < 4:                                  # try again in a few minutes, then give up for today
                try:
                    await db.pool.execute("UPDATE settings SET value='' WHERE key='backup_day'")
                except Exception:
                    pass
            else:
                fails = 0
                for a in admins:
                    try:
                        await bot.send_message(a, "⚠️ بکاپ خودکار امشب ۴ بار پشت‌سرهم ناموفق بود. از منو ← «💾 بکاپ دیتابیس» دستی بگیر و لاگ سرور رو چک کن.")
                    except Exception:
                        pass


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "restore":
        asyncio.run(restore(sys.argv[2]))
    else:
        print(__doc__)
