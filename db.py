import os
import time

import asyncpg

pool: asyncpg.Pool | None = None

DIAMONDS_PER_30_DAYS = 1500
DAILY_BONUS = 50
TRIAL_DAYS = float(os.getenv("TRIAL_DAYS", "1"))

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
            last_daily DOUBLE PRECISION NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS tx(
            id SERIAL PRIMARY KEY, user_id BIGINT, amount BIGINT, reason TEXT,
            ts TIMESTAMPTZ DEFAULT now());
        CREATE TABLE IF NOT EXISTS lobbies(
            id SERIAL PRIMARY KEY, chat_id BIGINT, bet BIGINT, kind INT,
            players BIGINT[] NOT NULL DEFAULT '{}');
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
    return await pool.fetchval("SELECT diamonds FROM wallets WHERE user_id=$1", uid) or 0


async def add(uid, amount, reason="", name=None) -> int:
    async with pool.acquire() as c:
        bal = await c.fetchval(
            "INSERT INTO wallets(user_id,name,diamonds) VALUES($1,$2,$3) "
            "ON CONFLICT(user_id) DO UPDATE SET diamonds=wallets.diamonds+$3, "
            "name=COALESCE($2,wallets.name) RETURNING diamonds", uid, name, amount)
        await c.execute("INSERT INTO tx(user_id,amount,reason) VALUES($1,$2,$3)", uid, amount, reason)
    return bal


async def try_spend(uid, amount, reason=""):
    """Atomic. Returns new balance, or None if not enough diamonds."""
    async with pool.acquire() as c:
        bal = await c.fetchval(
            "UPDATE wallets SET diamonds=diamonds-$2 WHERE user_id=$1 AND diamonds>=$2 RETURNING diamonds",
            uid, amount)
        if bal is not None:
            await c.execute("INSERT INTO tx(user_id,amount,reason) VALUES($1,$2,$3)", uid, -amount, reason)
    return bal


async def claim_daily(uid, name):
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
        "SELECT user_id,name,diamonds FROM wallets ORDER BY diamonds DESC LIMIT $1", n)


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
