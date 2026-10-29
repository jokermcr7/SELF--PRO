import os

import asyncpg

pool: asyncpg.Pool | None = None

PRICE_PER_1500 = 40000       # toman for 1500 diamonds
DAILY_BONUS = 50
WELCOME_BONUS = 100          # first /start
LOGIN_FEE = 30               # every self login
MIN_BUY = 30
MAX_BUY = 1_000_000
UNLIMITED = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.strip().isdigit()}
BIG = 10 ** 12

_ucache: dict = {}
_act: dict = {}


def is_unlimited(uid) -> bool:
    return uid in UNLIMITED


def fmt(uid, bal) -> str:
    return "نامحدود ♾" if uid in UNLIMITED else f"{int(bal):,}"


def price_for(n: int) -> int:
    """Toman price for n diamonds (1500 = 40,000), rounded up to the next 100 toman."""
    raw = -(-n * PRICE_PER_1500 // 1500)
    return -(-raw // 100) * 100


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
        CREATE TABLE IF NOT EXISTS wallets(
            user_id BIGINT PRIMARY KEY,
            name TEXT,
            diamonds BIGINT NOT NULL DEFAULT 0,
            last_daily DOUBLE PRECISION NOT NULL DEFAULT 0);
        ALTER TABLE wallets ADD COLUMN IF NOT EXISTS bonus_given BOOLEAN NOT NULL DEFAULT FALSE;
        CREATE TABLE IF NOT EXISTS tx(
            id SERIAL PRIMARY KEY, user_id BIGINT, amount BIGINT, reason TEXT,
            ts TIMESTAMPTZ DEFAULT now());
        CREATE TABLE IF NOT EXISTS lobbies(
            id SERIAL PRIMARY KEY, chat_id BIGINT, bet BIGINT, kind INT,
            players BIGINT[] NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS premium_emojis(
            user_id BIGINT, emoji TEXT, doc_id BIGINT NOT NULL,
            PRIMARY KEY(user_id, emoji));
        CREATE TABLE IF NOT EXISTS orders(
            id SERIAL PRIMARY KEY, user_id BIGINT, diamonds BIGINT, price BIGINT,
            status TEXT NOT NULL DEFAULT 'pending',
            created TIMESTAMPTZ DEFAULT now(), resolved TIMESTAMPTZ);
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


# ---------------- users ----------------
async def user_get(uid):
    return await pool.fetchrow("SELECT * FROM users WHERE user_id=$1", uid)


async def user_upsert(uid, phone, session):
    await pool.execute(
        "INSERT INTO users(user_id,phone,session,active) VALUES($1,$2,$3,TRUE) "
        "ON CONFLICT(user_id) DO UPDATE SET phone=EXCLUDED.phone, session=EXCLUDED.session, active=TRUE",
        uid, phone, session)
    _act[uid] = True


async def user_remove(uid):
    await pool.execute("UPDATE users SET active=FALSE, session='' WHERE user_id=$1", uid)
    _act[uid] = False


async def users_active_list():
    return await pool.fetch("SELECT * FROM users WHERE active AND session<>''")


async def users_count():
    total = await pool.fetchval("SELECT count(*) FROM users")
    active = await pool.fetchval("SELECT count(*) FROM users WHERE active AND session<>''")
    return total, active


async def user_is_active(uid) -> bool:
    if uid in UNLIMITED:
        return True
    if uid not in _act:
        row = await pool.fetchrow("SELECT active, session FROM users WHERE user_id=$1", uid)
        _act[uid] = bool(row and row["active"] and row["session"])
    return _act[uid]


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


async def uflag(uid, key, default=False):
    v = await uget(uid, key)
    return default if v is None else v == "1"


async def uset_flag(uid, key, on):
    await uset(uid, key, "1" if on else "0")


# ---------------- premium emojis ----------------
async def pe_all(uid) -> dict:
    rows = await pool.fetch("SELECT emoji, doc_id FROM premium_emojis WHERE user_id=$1", uid)
    return {r["emoji"]: r["doc_id"] for r in rows}


async def pe_set(uid, emoji, doc_id):
    await pool.execute(
        "INSERT INTO premium_emojis(user_id,emoji,doc_id) VALUES($1,$2,$3) "
        "ON CONFLICT(user_id,emoji) DO UPDATE SET doc_id=EXCLUDED.doc_id", uid, emoji, doc_id)


async def pe_del(uid, emoji):
    await pool.execute("DELETE FROM premium_emojis WHERE user_id=$1 AND emoji=$2", uid, emoji)


async def pe_clear(uid):
    await pool.execute("DELETE FROM premium_emojis WHERE user_id=$1", uid)


# ---------------- diamonds ----------------
async def ensure_wallet(uid, name=None):
    await pool.execute(
        "INSERT INTO wallets(user_id,name) VALUES($1,$2) "
        "ON CONFLICT(user_id) DO UPDATE SET name=COALESCE($2,wallets.name)", uid, name)


async def balance(uid) -> int:
    if uid in UNLIMITED:
        return BIG
    return await pool.fetchval("SELECT diamonds FROM wallets WHERE user_id=$1", uid) or 0


async def add(uid, amount, reason="", name=None) -> int:
    async with pool.acquire() as c:
        bal = await c.fetchval(
            "INSERT INTO wallets(user_id,name,diamonds) VALUES($1,$2,$3) "
            "ON CONFLICT(user_id) DO UPDATE SET diamonds=wallets.diamonds+$3, "
            "name=COALESCE($2,wallets.name) RETURNING diamonds", uid, name, amount)
        await c.execute("INSERT INTO tx(user_id,amount,reason) VALUES($1,$2,$3)", uid, amount, reason)
    return BIG if uid in UNLIMITED else bal


async def try_spend(uid, amount, reason=""):
    """Atomic. Returns new balance, or None if not enough diamonds. Admins are never charged."""
    if uid in UNLIMITED:
        await pool.execute("INSERT INTO tx(user_id,amount,reason) VALUES($1,$2,$3)", uid, 0, reason + "_free")
        return BIG
    async with pool.acquire() as c:
        bal = await c.fetchval(
            "UPDATE wallets SET diamonds=diamonds-$2 WHERE user_id=$1 AND diamonds>=$2 RETURNING diamonds",
            uid, amount)
        if bal is not None:
            await c.execute("INSERT INTO tx(user_id,amount,reason) VALUES($1,$2,$3)", uid, -amount, reason)
    return bal


async def claim_welcome(uid, name) -> bool:
    """One-time first-entry gift."""
    await ensure_wallet(uid, name)
    async with pool.acquire() as c:
        got = await c.fetchval(
            "UPDATE wallets SET diamonds=diamonds+$2, bonus_given=TRUE "
            "WHERE user_id=$1 AND NOT bonus_given RETURNING diamonds", uid, WELCOME_BONUS)
        if got is not None:
            await c.execute("INSERT INTO tx(user_id,amount,reason) VALUES($1,$2,'welcome')", uid, WELCOME_BONUS)
    return got is not None


async def claim_daily(uid, name):
    import time
    now = time.time()
    await ensure_wallet(uid, name)
    async with pool.acquire() as c:
        last = await c.fetchval("SELECT last_daily FROM wallets WHERE user_id=$1", uid)
        if now - last < 86400:
            return None, int(86400 - (now - last))
        bal = await c.fetchval(
            "UPDATE wallets SET diamonds=diamonds+$2, last_daily=$3 WHERE user_id=$1 RETURNING diamonds",
            uid, DAILY_BONUS, now)
        await c.execute("INSERT INTO tx(user_id,amount,reason) VALUES($1,$2,'daily')", uid, DAILY_BONUS)
    return bal, 0


async def top(n=10):
    return await pool.fetch(
        "SELECT user_id,name,diamonds FROM wallets WHERE user_id <> ALL($2::bigint[]) "
        "ORDER BY diamonds DESC LIMIT $1", n, list(UNLIMITED))


# ---------------- purchase orders ----------------
async def order_create(uid, diamonds, price) -> int:
    return await pool.fetchval(
        "INSERT INTO orders(user_id,diamonds,price) VALUES($1,$2,$3) RETURNING id", uid, diamonds, price)


async def order_resolve(oid, status):
    """Atomic: only a pending order can be resolved (prevents double charging)."""
    return await pool.fetchrow(
        "UPDATE orders SET status=$2, resolved=now() WHERE id=$1 AND status='pending' RETURNING *", oid, status)


# ---------------- lobbies (persisted so bets are refunded after a crash) ----------------
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
