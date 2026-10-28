import os
import time

import asyncpg

pool: asyncpg.Pool | None = None

DIAMONDS_PER_30_DAYS = 1500
DAILY_BONUS = 50
WELCOME_BONUS = int(os.getenv("WELCOME_BONUS", "100"))   # first /start
REF_BONUS = int(os.getenv("REF_BONUS", "50"))            # inviter reward
LOGIN_COST = int(os.getenv("LOGIN_COST", "30"))          # per self login
PACK_DIAMONDS = 1500
PACK_TOMAN = int(os.getenv("PACK_TOMAN", "40000"))       # 1500 diamonds = 40,000 toman
TRIAL_DAYS = float(os.getenv("TRIAL_DAYS", "1"))

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

_ucache: dict = {}
_exp: dict = {}


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


# ---------------- users / subscription ----------------
async def user_get(uid):
    return await pool.fetchrow("SELECT * FROM users WHERE user_id=$1", uid)


async def user_upsert(uid, phone, session):
    exp = time.time() + TRIAL_DAYS * 86400
    row = await pool.fetchrow(
        "INSERT INTO users(user_id,phone,session,expires_at,active) VALUES($1,$2,$3,$4,TRUE) "
        "ON CONFLICT(user_id) DO UPDATE SET phone=EXCLUDED.phone, session=EXCLUDED.session, active=TRUE "
        "RETURNING expires_at", uid, phone, session, exp)
    _exp[uid] = row["expires_at"]


async def user_remove(uid):
    await pool.execute("UPDATE users SET active=FALSE, session='' WHERE user_id=$1", uid)
    _exp[uid] = 0


async def users_active_list():
    return await pool.fetch("SELECT * FROM users WHERE active AND session<>''")


async def users_count():
    total = await pool.fetchval("SELECT count(*) FROM users")
    active = await pool.fetchval("SELECT count(*) FROM users WHERE active AND expires_at>$1", time.time())
    return total, active


async def expires_at(uid) -> float:
    if uid in ADMINS:
        return FOREVER
    if uid not in _exp:
        row = await pool.fetchrow("SELECT expires_at, active FROM users WHERE user_id=$1", uid)
        _exp[uid] = (row["expires_at"] if row and row["active"] else 0)
    return _exp[uid]


async def user_is_active(uid) -> bool:
    return await expires_at(uid) > time.time()


async def extend_days(uid, days):
    cur = max(await expires_at(uid), time.time())
    new = cur + days * 86400
    await pool.execute("UPDATE users SET expires_at=$2 WHERE user_id=$1", uid, new)
    _exp[uid] = new
    await uset(uid, "exp_notified", "0")


async def renew(uid) -> bool:
    if await try_spend(uid, DIAMONDS_PER_30_DAYS, "renew") is None:
        return False
    await extend_days(uid, 30)
    return True


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


async def balance(uid) -> int:
    if uid in ADMINS:
        return UNLIMITED
    return await pool.fetchval("SELECT diamonds FROM wallets WHERE user_id=$1", uid) or 0


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


# ---------------- purchase receipts ----------------
async def receipt_create(uid, amount):
    return await pool.fetchval("INSERT INTO receipts(user_id,amount) VALUES($1,$2) RETURNING id", uid, amount)


async def receipts_pending(uid) -> int:
    return await pool.fetchval("SELECT count(*) FROM receipts WHERE user_id=$1 AND status='pending'", uid) or 0


async def receipt_resolve(rid, status):
    """Atomic pending -> approved/rejected. Returns the row, or None if already handled."""
    return await pool.fetchrow(
        "UPDATE receipts SET status=$2 WHERE id=$1 AND status='pending' RETURNING user_id,amount", rid, status)


async def claim_daily(uid, name):
    if uid in ADMINS:
        return UNLIMITED, 0
    now = time.time()
    await ensure_wallet(uid, name)
    bal = await pool.fetchval(
        "UPDATE wallets SET diamonds=diamonds+$2, last_daily=$3 "
        "WHERE user_id=$1 AND last_daily<=$3-86400 RETURNING diamonds", uid, DAILY_BONUS, now)
    if bal is None:
        last = await pool.fetchval("SELECT last_daily FROM wallets WHERE user_id=$1", uid) or 0
        return None, max(int(86400 - (now - last)), 1)
    await pool.execute("INSERT INTO tx(user_id,amount,reason) VALUES($1,$2,'daily')", uid, DAILY_BONUS)
    return bal, 0


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
