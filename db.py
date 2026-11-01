import os
import time

import asyncpg

pool: asyncpg.Pool | None = None

# ---- hourly billing: 1400 diamonds = 30 days (720 h)  ->  35/18 = 1.9444.. diamonds per hour.
# Exact integer maths: everything is counted in 1/18-diamond "units" (35 units per hour, 18 units per diamond).
DIAMONDS_PER_30_DAYS = int(os.getenv("DIAMONDS_PER_30_DAYS", "1400"))
HOURS_30_DAYS = 30 * 24
_G = __import__("math").gcd(DIAMONDS_PER_30_DAYS, HOURS_30_DAYS)
UNIT = HOURS_30_DAYS // _G                      # units per diamond   (1400/720 -> 18)
RATE = DIAMONDS_PER_30_DAYS // _G               # units per hour      (1400/720 -> 35)
RATE_PER_HOUR = DIAMONDS_PER_30_DAYS / HOURS_30_DAYS
RATE_PER_DAY = RATE_PER_HOUR * 24
WELCOME_BONUS = int(os.getenv("WELCOME_BONUS", "100"))   # first /start
REF_BONUS = int(os.getenv("REF_BONUS", "50"))            # inviter reward
PACK_DIAMONDS = 1500
PACK_TOMAN = int(os.getenv("PACK_TOMAN", "40000"))       # 1500 diamonds = 40,000 toman

UNLIMITED = 10 ** 12          # owner(s) never run out of diamonds
ADMINS: set[int] = set()      # filled from main.py (ADMIN_IDS)
FOREVER = 4102444800.0        # year 2100: owner subscription never expires


def is_admin(uid) -> bool:
    return uid in ADMINS


def fmt(n) -> str:
    return "♾ نامحدود" if n >= UNLIMITED else f"{int(n):,}"


def price_toman(diamonds: int) -> int:
    """Exact price rounded UP to the next 100 toman (1500 -> 40,000)."""
    raw = -(-diamonds * PACK_TOMAN // PACK_DIAMONDS)
    return -(-raw // 100) * 100

USD_TOMAN = int(os.getenv("USD_TOMAN", "272000"))        # toman per 1 USD (used only to show the $ value)


def value_toman(diamonds: int) -> int:
    return round(diamonds * PACK_TOMAN / PACK_DIAMONDS)


def value_text(diamonds: int) -> str:
    """'92,775T | 0.34$' - what the diamonds are worth (owner: unlimited)."""
    if diamonds >= UNLIMITED:
        return "∞"
    t = value_toman(diamonds)
    return f"{t:,}T | {t / USD_TOMAN:.2f}$"


_ucache: dict = {}


async def init():
    global pool
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=8)
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users(
            user_id BIGINT PRIMARY KEY,
            phone TEXT,
            session TEXT NOT NULL DEFAULT '',
            expires_at DOUBLE PRECISION NOT NULL DEFAULT 0,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created TIMESTAMPTZ DEFAULT now());
        CREATE TABLE IF NOT EXISTS user_settings(
            user_id BIGINT, key TEXT, value TEXT NOT NULL,
            PRIMARY KEY(user_id, key));
        CREATE TABLE IF NOT EXISTS autoreplies(
            user_id BIGINT, keyword TEXT, response TEXT NOT NULL,
            PRIMARY KEY(user_id, keyword));
        CREATE TABLE IF NOT EXISTS wallets(
            user_id BIGINT PRIMARY KEY,
            name TEXT,
            diamonds BIGINT NOT NULL DEFAULT 0,
            last_daily DOUBLE PRECISION NOT NULL DEFAULT 0,
            welcomed BOOLEAN NOT NULL DEFAULT FALSE,
            ref_by BIGINT);
        CREATE TABLE IF NOT EXISTS tx(
            id SERIAL PRIMARY KEY, user_id BIGINT, amount BIGINT, reason TEXT,
            ts TIMESTAMPTZ DEFAULT now());
        CREATE TABLE IF NOT EXISTS lobbies(
            id SERIAL PRIMARY KEY, chat_id BIGINT, bet BIGINT, kind INT,
            players BIGINT[] NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS claims(key TEXT PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now());
        ALTER TABLE wallets ADD COLUMN IF NOT EXISTS welcomed BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE wallets ADD COLUMN IF NOT EXISTS ref_by BIGINT;
        CREATE TABLE IF NOT EXISTS codes(
            code TEXT PRIMARY KEY, amount BIGINT NOT NULL, max_uses INT NOT NULL,
            used INT NOT NULL DEFAULT 0, created TIMESTAMPTZ DEFAULT now());
        CREATE TABLE IF NOT EXISTS code_uses(
            code TEXT, user_id BIGINT, PRIMARY KEY(code, user_id));
        CREATE TABLE IF NOT EXISTS receipts(
            id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, amount BIGINT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', created TIMESTAMPTZ DEFAULT now());
        ALTER TABLE receipts ADD COLUMN IF NOT EXISTS price BIGINT NOT NULL DEFAULT 0;
        ALTER TABLE receipts ADD COLUMN IF NOT EXISTS dcode TEXT;
        CREATE TABLE IF NOT EXISTS discounts(
            code TEXT PRIMARY KEY, percent INT NOT NULL, max_uses INT NOT NULL,
            used INT NOT NULL DEFAULT 0, created TIMESTAMPTZ DEFAULT now());
        CREATE TABLE IF NOT EXISTS discount_uses(
            code TEXT, user_id BIGINT, PRIMARY KEY(code, user_id));
        """
    )


# ---------------- global settings ----------------
async def get(key, default=None):
    v = await pool.fetchval("SELECT value FROM settings WHERE key=$1", key)
    return default if v is None else v


async def set_(key, value):
    await pool.execute(
        "INSERT INTO settings(key,value) VALUES($1,$2) "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", key, str(value))


# ---------------- users / self accounts ----------------
async def user_get(uid):
    return await pool.fetchrow("SELECT * FROM users WHERE user_id=$1", uid)


async def user_upsert(uid, phone, session):
    await pool.execute(
        "INSERT INTO users(user_id,phone,session,expires_at,active) VALUES($1,$2,$3,0,TRUE) "
        "ON CONFLICT(user_id) DO UPDATE SET phone=EXCLUDED.phone, session=EXCLUDED.session, active=TRUE",
        uid, phone, session)
    _funded.pop(uid, None)


async def user_remove(uid):
    await pool.execute("UPDATE users SET active=FALSE, session='' WHERE user_id=$1", uid)
    _funded.pop(uid, None)


async def users_active_list():
    return await pool.fetch("SELECT * FROM users WHERE active AND session<>''")


async def users_count():
    total = await pool.fetchval("SELECT count(*) FROM wallets")
    active = await pool.fetchval("SELECT count(*) FROM users WHERE active AND session<>''")
    return total, active


# ---------------- hourly billing ----------------
# Pay-as-you-go: while a self is ON, every full hour of running time costs RATE/UNIT diamonds (1400 per 30 days).
#   credit = prepaid units left over from the last whole-diamond charge (stored per user)
#   acc    = seconds already used inside the current hour (kept in memory, saved every few minutes)
# remaining time = (diamonds*UNIT + credit) * 3600 / RATE  -  acc            (exact to the second)
acc: dict[int, float] = {}


async def credit(uid) -> int:
    return int(await uget(uid, "bill_credit", "0") or 0)


async def get_acc(uid) -> float:
    if uid not in acc:
        acc[uid] = float(await uget(uid, "bill_acc", "0") or 0)
    return acc[uid]


async def save_acc(uid):
    if uid in acc:
        await uset(uid, "bill_acc", f"{acc[uid]:.1f}")


async def time_units(uid) -> int:
    return await balance_raw(uid) * UNIT + await credit(uid)


def fmt_secs(sec: int) -> str:
    sec = max(int(sec), 0)
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    parts = []
    if d:
        parts.append(f"{d} روز")
    if h:
        parts.append(f"{h} ساعت")
    if m or not parts:
        parts.append(f"{m} دقیقه")
    return " و ".join(parts)


async def remaining_seconds(uid) -> int:
    """Exact seconds of self time the user can still use (owner: practically infinite)."""
    if uid in ADMINS:
        return 10 ** 9
    return max(int(await time_units(uid) * 3600 // RATE - await get_acc(uid)), 0)


async def time_left_text(uid) -> str:
    """Full text: X روز و Y ساعت و Z دقیقه."""
    return "نامحدود" if uid in ADMINS else fmt_secs(await remaining_seconds(uid))


async def time_left_short(uid) -> str:
    """Short text for button labels."""
    if uid in ADMINS:
        return "نامحدود"
    sec = await remaining_seconds(uid)
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    if d:
        return f"{d} روز" + (f" و {h} ساعت" if h else "")
    if h:
        return f"{h} ساعت" + (f" و {m} دقیقه" if m else "")
    return f"{m} دقیقه"


async def bill_hours(uid, hours: int = 1):
    """Charge `hours` of self time exactly (1400 diamonds = 720 h). True when paid, False if not enough diamonds."""
    if uid in ADMINS or hours <= 0:
        return True
    need = RATE * hours
    async with pool.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow(
                "SELECT value FROM user_settings WHERE user_id=$1 AND key='bill_credit' FOR UPDATE", uid)
            cr = int(row["value"]) if row else 0
            take = 0
            if cr < need:
                take = -(-(need - cr) // UNIT)          # whole diamonds needed, rounded up
                bal = await c.fetchval(
                    "UPDATE wallets SET diamonds=diamonds-$2 WHERE user_id=$1 AND diamonds>=$2 RETURNING diamonds",
                    uid, take)
                if bal is None:
                    return False
                await c.execute("INSERT INTO tx(user_id,amount,reason) VALUES($1,$2,'self_hourly')", uid, -take)
            cr = cr + take * UNIT - need
            await c.execute(
                "INSERT INTO user_settings(user_id,key,value) VALUES($1,'bill_credit',$2) "
                "ON CONFLICT(user_id,key) DO UPDATE SET value=EXCLUDED.value", uid, str(cr))
    _ucache[(uid, "bill_credit")] = str(cr)
    _funded.pop(uid, None)
    return True


async def has_funds(uid, hours: int = 1) -> bool:
    """Enough diamonds for at least `hours` more hours (used before logging a self in)."""
    if uid in ADMINS:
        return True
    return await remaining_seconds(uid) >= 3600 * hours


_funded: dict = {}      # uid -> (ts, bool)  short cache, because every incoming message asks


async def user_is_active(uid) -> bool:
    """Self may work: owner, or an active self account that still has paid time left."""
    if uid in ADMINS:
        return True
    hit = _funded.get(uid)
    if hit and time.time() - hit[0] < 15:
        return hit[1]
    row = await pool.fetchrow("SELECT active FROM users WHERE user_id=$1", uid)
    ok = bool(row and row["active"] and await remaining_seconds(uid) > 0)
    _funded[uid] = (time.time(), ok)
    return ok


# ---------------- per-user settings (cached) ----------------
async def uget(uid, key, default=None):
    k = (uid, key)
    if k not in _ucache:
        _ucache[k] = await pool.fetchval(
            "SELECT value FROM user_settings WHERE user_id=$1 AND key=$2", uid, key)
    v = _ucache[k]
    return default if v is None else v


async def uset(uid, key, value):
    await pool.execute(
        "INSERT INTO user_settings(user_id,key,value) VALUES($1,$2,$3) "
        "ON CONFLICT(user_id,key) DO UPDATE SET value=EXCLUDED.value", uid, key, str(value))
    _ucache[(uid, key)] = str(value)


async def udel(uid, key):
    await pool.execute("DELETE FROM user_settings WHERE user_id=$1 AND key=$2", uid, key)
    _ucache.pop((uid, key), None)


async def uprefix(uid, prefix):
    return await pool.fetch("SELECT key,value FROM user_settings WHERE user_id=$1 AND key LIKE $2 ORDER BY key",
                            uid, prefix + "%")


async def uflag(uid, key, default=False):
    v = await uget(uid, key)
    return default if v is None else v == "1"


async def uset_flag(uid, key, on):
    await uset(uid, key, "1" if on else "0")


# ---------------- keyword auto replies ----------------
async def ar_add(uid, kw, resp):
    await pool.execute(
        "INSERT INTO autoreplies(user_id,keyword,response) VALUES($1,$2,$3) "
        "ON CONFLICT(user_id,keyword) DO UPDATE SET response=EXCLUDED.response", uid, kw.lower(), resp)


async def ar_del(uid, kw):
    await pool.execute("DELETE FROM autoreplies WHERE user_id=$1 AND keyword=$2", uid, kw.lower())


async def ar_list(uid):
    return await pool.fetch("SELECT keyword,response FROM autoreplies WHERE user_id=$1", uid)


# ---------------- diamonds ----------------
async def ensure_wallet(uid, name=None):
    await pool.execute(
        "INSERT INTO wallets(user_id,name) VALUES($1,$2) "
        "ON CONFLICT(user_id) DO UPDATE SET name=COALESCE($2,wallets.name)", uid, name)


async def balance_raw(uid) -> int:
    return await pool.fetchval("SELECT diamonds FROM wallets WHERE user_id=$1", uid) or 0


async def balance(uid) -> int:
    if uid in ADMINS:
        return UNLIMITED
    return await balance_raw(uid)


async def add(uid, amount, reason="", name=None) -> int:
    if uid in ADMINS:                       # owner is unlimited: nothing to add
        await ensure_wallet(uid, name)
        return UNLIMITED
    async with pool.acquire() as c:
        bal = await c.fetchval(
            "INSERT INTO wallets(user_id,name,diamonds) VALUES($1,$2,$3) "
            "ON CONFLICT(user_id) DO UPDATE SET diamonds=wallets.diamonds+$3, "
            "name=COALESCE($2,wallets.name) RETURNING diamonds", uid, name, amount)
        await c.execute("INSERT INTO tx(user_id,amount,reason) VALUES($1,$2,$3)", uid, amount, reason)
    _funded.pop(uid, None)
    return bal


async def try_spend(uid, amount, reason=""):
    """Atomic. Returns new balance, or None if not enough diamonds. Owner never pays."""
    if uid in ADMINS:
        return UNLIMITED
    async with pool.acquire() as c:
        bal = await c.fetchval(
            "UPDATE wallets SET diamonds=diamonds-$2 WHERE user_id=$1 AND diamonds>=$2 RETURNING diamonds",
            uid, amount)
        if bal is not None:
            await c.execute("INSERT INTO tx(user_id,amount,reason) VALUES($1,$2,$3)", uid, -amount, reason)
    _funded.pop(uid, None)
    return bal


async def transfer(src, dst, amount, dst_name=None):
    """Move diamonds in ONE transaction. Returns (src_balance, dst_balance) or None if src is short."""
    if amount <= 0 or src == dst:
        return None
    async with pool.acquire() as c:
        async with c.transaction():
            if src in ADMINS:
                sb = UNLIMITED
            else:
                sb = await c.fetchval(
                    "UPDATE wallets SET diamonds=diamonds-$2 WHERE user_id=$1 AND diamonds>=$2 RETURNING diamonds",
                    src, amount)
                if sb is None:
                    return None
                await c.execute("INSERT INTO tx(user_id,amount,reason) VALUES($1,$2,'transfer_out')", src, -amount)
            if dst in ADMINS:
                await c.execute("INSERT INTO wallets(user_id,name) VALUES($1,$2) ON CONFLICT DO NOTHING", dst, dst_name)
                db_ = UNLIMITED
            else:
                db_ = await c.fetchval(
                    "INSERT INTO wallets(user_id,name,diamonds) VALUES($1,$2,$3) "
                    "ON CONFLICT(user_id) DO UPDATE SET diamonds=wallets.diamonds+$3, "
                    "name=COALESCE($2,wallets.name) RETURNING diamonds", dst, dst_name, amount)
                await c.execute("INSERT INTO tx(user_id,amount,reason) VALUES($1,$2,'transfer_in')", dst, amount)
    _funded.pop(src, None)
    _funded.pop(dst, None)
    return sb, db_


async def welcome_bonus(uid, name=None, ref=None):
    """First entry to the bot: WELCOME_BONUS diamonds, once. Returns (granted, ref_rewarded)."""
    await ensure_wallet(uid, name)
    got = await pool.fetchval(
        "UPDATE wallets SET welcomed=TRUE WHERE user_id=$1 AND NOT welcomed RETURNING user_id", uid)
    if got is None:
        return False, False
    await add(uid, WELCOME_BONUS, "welcome")
    rewarded = False
    if ref and ref != uid and await pool.fetchval("SELECT 1 FROM wallets WHERE user_id=$1", ref):
        await pool.execute("UPDATE wallets SET ref_by=$2 WHERE user_id=$1", uid, ref)
        await add(ref, REF_BONUS, "referral")
        rewarded = True
    return True, rewarded


async def history(uid, n=10):
    return await pool.fetch("SELECT amount,reason,ts FROM tx WHERE user_id=$1 ORDER BY id DESC LIMIT $2", uid, n)


async def invited_count(uid) -> int:
    return await pool.fetchval("SELECT count(*) FROM wallets WHERE ref_by=$1", uid) or 0


# ---------------- gift codes ----------------
async def code_create(code, amount, max_uses):
    await pool.execute(
        "INSERT INTO codes(code,amount,max_uses) VALUES($1,$2,$3) "
        "ON CONFLICT(code) DO UPDATE SET amount=$2, max_uses=$3, used=0", code.lower(), amount, max_uses)
    await pool.execute("DELETE FROM code_uses WHERE code=$1", code.lower())


async def code_redeem(uid, code):
    """Returns amount, or an error string: 'none' | 'used' | 'full'."""
    code = code.lower()
    async with pool.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow("SELECT amount,max_uses,used FROM codes WHERE code=$1 FOR UPDATE", code)
            if not row:
                return "none"
            if row["used"] >= row["max_uses"]:
                return "full"
            if await c.fetchval("SELECT 1 FROM code_uses WHERE code=$1 AND user_id=$2", code, uid):
                return "used"
            await c.execute("INSERT INTO code_uses(code,user_id) VALUES($1,$2)", code, uid)
            await c.execute("UPDATE codes SET used=used+1 WHERE code=$1", code)
    await add(uid, row["amount"], "code:" + code)
    return row["amount"]


async def code_list(n=15):
    return await pool.fetch("SELECT code,amount,max_uses,used FROM codes ORDER BY created DESC LIMIT $1", n)


async def code_delete(code) -> bool:
    code = code.lower()
    await pool.execute("DELETE FROM code_uses WHERE code=$1", code)
    return (await pool.execute("DELETE FROM codes WHERE code=$1", code)).endswith("1")


# ---------------- admin: every user ----------------
async def all_users(offset=0, limit=8):
    """All people who ever started the bot (wallet) + their self state, newest self first."""
    return await pool.fetch(
        "SELECT w.user_id, w.name, w.diamonds, (u.user_id IS NOT NULL AND u.active AND u.session<>'') AS has_self "
        "FROM wallets w LEFT JOIN users u ON u.user_id=w.user_id "
        "ORDER BY has_self DESC, w.user_id DESC OFFSET $1 LIMIT $2", offset, limit)


async def all_users_count() -> int:
    return await pool.fetchval("SELECT count(*) FROM wallets") or 0


async def user_full(uid):
    """Everything the admin sees about one user."""
    return {
        "wallet": await pool.fetchrow("SELECT * FROM wallets WHERE user_id=$1", uid),
        "user": await pool.fetchrow("SELECT user_id,phone,active,created,(session<>'') AS has_session FROM users WHERE user_id=$1", uid),
        "settings": {r["key"]: r["value"] for r in await pool.fetch(
            "SELECT key,value FROM user_settings WHERE user_id=$1 AND key NOT LIKE 'emoji:%' AND key NOT LIKE 'note:%'", uid)},
        "replies": await pool.fetchval("SELECT count(*) FROM autoreplies WHERE user_id=$1", uid),
        "invited": await invited_count(uid),
        "tx": await history(uid, 5),
    }


# ---------------- purchase receipts ----------------
async def receipt_create(uid, amount, price=0, dcode=None):
    return await pool.fetchval(
        "INSERT INTO receipts(user_id,amount,price,dcode) VALUES($1,$2,$3,$4) RETURNING id", uid, amount, price, dcode)


async def receipts_pending(uid) -> int:
    return await pool.fetchval("SELECT count(*) FROM receipts WHERE user_id=$1 AND status='pending'", uid) or 0


async def receipt_resolve(rid, status):
    """Atomic pending -> approved/rejected. Returns the row, or None if already handled.
    A rejected receipt gives its discount code back to the user."""
    row = await pool.fetchrow(
        "UPDATE receipts SET status=$2 WHERE id=$1 AND status='pending' RETURNING user_id,amount,price,dcode", rid, status)
    if row and status == "rejected" and row["dcode"]:
        await discount_release(row["dcode"], row["user_id"])
    return row


# ---------------- discount codes for buying diamonds (separate from gift codes) ----------------
def discounted(price: int, percent: int) -> int:
    """Price after `percent`% off, rounded UP to the next 100 toman (never below 100)."""
    new = -(-price * (100 - percent) // 100)
    return max(-(-new // 100) * 100, 100) if price else 0


async def discount_create(code, percent, max_uses):
    code = code.lower()
    await pool.execute(
        "INSERT INTO discounts(code,percent,max_uses) VALUES($1,$2,$3) "
        "ON CONFLICT(code) DO UPDATE SET percent=$2, max_uses=$3, used=0", code, percent, max_uses)
    await pool.execute("DELETE FROM discount_uses WHERE code=$1", code)


async def discount_check(code, uid):
    """Read-only check. Returns (percent, None) or (None, 'none'|'used'|'full')."""
    code = (code or "").strip().lower()
    row = await pool.fetchrow("SELECT percent,max_uses,used FROM discounts WHERE code=$1", code)
    if not row:
        return None, "none"
    if await pool.fetchval("SELECT 1 FROM discount_uses WHERE code=$1 AND user_id=$2", code, uid):
        return None, "used"
    if row["used"] >= row["max_uses"]:
        return None, "full"
    return row["percent"], None


async def discount_consume(code, uid):
    """Atomically use one slot of the code for this user (when the receipt is sent). Same return as discount_check."""
    code = (code or "").strip().lower()
    async with pool.acquire() as c:
        async with c.transaction():
            row = await c.fetchrow("SELECT percent,max_uses,used FROM discounts WHERE code=$1 FOR UPDATE", code)
            if not row:
                return None, "none"
            if await c.fetchval("SELECT 1 FROM discount_uses WHERE code=$1 AND user_id=$2", code, uid):
                return None, "used"
            if row["used"] >= row["max_uses"]:
                return None, "full"
            await c.execute("INSERT INTO discount_uses(code,user_id) VALUES($1,$2)", code, uid)
            await c.execute("UPDATE discounts SET used=used+1 WHERE code=$1", code)
    return row["percent"], None


async def discount_release(code, uid):
    """Receipt rejected: the user may use the code again."""
    code = (code or "").lower()
    async with pool.acquire() as c:
        async with c.transaction():
            gone = await c.fetchval("DELETE FROM discount_uses WHERE code=$1 AND user_id=$2 RETURNING 1", code, uid)
            if gone:
                await c.execute("UPDATE discounts SET used=GREATEST(used-1,0) WHERE code=$1", code)


async def discount_list(n=8):
    return await pool.fetch("SELECT code,percent,max_uses,used FROM discounts ORDER BY created DESC LIMIT $1", n)


async def discount_delete(code) -> bool:
    code = code.lower()
    await pool.execute("DELETE FROM discount_uses WHERE code=$1", code)
    return (await pool.execute("DELETE FROM discounts WHERE code=$1", code)).endswith("1")


async def top(n=10):
    return await pool.fetch(
        "SELECT user_id,name,diamonds FROM wallets WHERE user_id<>ALL($2::bigint[]) "
        "ORDER BY diamonds DESC LIMIT $1", n, list(ADMINS))


# ---------------- game lobbies (persisted so bets can be refunded after a crash) ----------------
async def lobby_create(chat_id, bet, kind, creator):
    return await pool.fetchval(
        "INSERT INTO lobbies(chat_id,bet,kind,players) VALUES($1,$2,$3,ARRAY[$4::bigint]) RETURNING id",
        chat_id, bet, kind, creator)


async def lobby_add(lid, uid):
    await pool.execute("UPDATE lobbies SET players=array_append(players,$2) WHERE id=$1", lid, uid)


async def lobby_delete(lid):
    await pool.execute("DELETE FROM lobbies WHERE id=$1", lid)


async def lobby_all():
    return await pool.fetch("SELECT * FROM lobbies")


async def claim_event(key: str) -> bool:
    """True only for the FIRST caller of this key (shared through the database), so one group message is
    answered exactly once even if two bot processes / duplicate updates see it. Fails open on DB errors."""
    try:
        ok = await pool.fetchval("INSERT INTO claims(key) VALUES($1) ON CONFLICT DO NOTHING RETURNING 1", key)
        if int(time.time()) % 40 == 0:
            await pool.execute("DELETE FROM claims WHERE ts < now() - interval '1 hour'")
        return bool(ok)
    except Exception:
        return True
