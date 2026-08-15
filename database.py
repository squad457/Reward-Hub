"""
database.py
aiosqlite data layer for the Reward Hub Telegram Mini App.
"""

import aiosqlite
import secrets
import time
import os
from contextlib import asynccontextmanager

DB_PATH = os.environ.get("DATABASE_PATH", "/data/rewardhub.db" if os.path.exists("/data") else "rewardhub.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id     INTEGER UNIQUE NOT NULL,
    username        TEXT,
    first_name      TEXT,
    photo_url       TEXT,
    gems            INTEGER NOT NULL DEFAULT 0,
    balance_usdt    REAL NOT NULL DEFAULT 0,
    referral_code   TEXT UNIQUE NOT NULL,
    referred_by     INTEGER,
    daily_streak    INTEGER NOT NULL DEFAULT 0,
    last_daily_claim INTEGER,
    spins_today     INTEGER NOT NULL DEFAULT 0,
    spins_reset_at  INTEGER,
    bonus_spins     INTEGER NOT NULL DEFAULT 0,
    is_vip          INTEGER NOT NULL DEFAULT 0,
    vip_expires_at  INTEGER,
    created_at      INTEGER NOT NULL,
    device_fingerprint TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    description     TEXT,
    reward_gems     INTEGER NOT NULL,
    task_type       TEXT NOT NULL DEFAULT 'special',
    link            TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    sort_order      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS user_tasks (
    user_id         INTEGER NOT NULL,
    task_id         INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    claimed_at      INTEGER,
    PRIMARY KEY (user_id, task_id)
);

CREATE TABLE IF NOT EXISTS spin_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    reward_gems     INTEGER NOT NULL,
    segment_id      INTEGER,
    created_at      INTEGER NOT NULL
);

-- Admin-configurable wheel. Segments outside [spin_config.min,max] are is_real=0:
-- shown for visual excitement, but the spin picker never selects them as the outcome.
CREATE TABLE IF NOT EXISTS wheel_segments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT NOT NULL,
    value_gems      INTEGER NOT NULL,
    color           TEXT NOT NULL DEFAULT '#12B886',
    is_real         INTEGER NOT NULL DEFAULT 1,
    sort_order      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS spin_config (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    min_reward          INTEGER NOT NULL DEFAULT 10,
    max_reward          INTEGER NOT NULL DEFAULT 500,
    max_spins_per_day   INTEGER NOT NULL DEFAULT 8
);

CREATE TABLE IF NOT EXISTS referrals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id     INTEGER NOT NULL,
    referred_id     INTEGER NOT NULL,
    bonus_gems      INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_rewards (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    day_1           INTEGER NOT NULL DEFAULT 80,
    day_2           INTEGER NOT NULL DEFAULT 80,
    day_3           INTEGER NOT NULL DEFAULT 200,
    day_4           INTEGER NOT NULL DEFAULT 90,
    day_5           INTEGER NOT NULL DEFAULT 90,
    day_6           INTEGER NOT NULL DEFAULT 90,
    day_7           INTEGER NOT NULL DEFAULT 6000
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL,
    amount_usdt     REAL NOT NULL,
    method          TEXT NOT NULL,
    destination     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS gift_codes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT UNIQUE NOT NULL,
    reward_gems     INTEGER NOT NULL DEFAULT 0,
    reward_spins    INTEGER NOT NULL DEFAULT 0,
    reward_usdt     REAL NOT NULL DEFAULT 0.0,
    max_uses        INTEGER NOT NULL DEFAULT 1,
    used_count      INTEGER NOT NULL DEFAULT 0,
    expires_at      INTEGER,
    active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS gift_code_redemptions (
    user_id         INTEGER NOT NULL,
    gift_code_id    INTEGER NOT NULL,
    redeemed_at     INTEGER NOT NULL,
    PRIMARY KEY (user_id, gift_code_id)
);

CREATE TABLE IF NOT EXISTS admins (
    api_key         TEXT PRIMARY KEY
);

-- Simple key/value store for everything an admin should be able to edit
-- without a redeploy: AdsGram block id, bot username, gems->USDT rate, etc.
CREATE TABLE IF NOT EXISTS app_settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);
CREATE INDEX IF NOT EXISTS idx_withdrawals_user ON withdrawals(user_id);
"""

DEFAULT_SETTINGS = {
    "adsgram_block_id": "",
    "bot_username": "",
    "gems_per_usdt": "1000",
    "app_name": "Reward Hub",
    "referral_reward_gems": "250",
    "referral_reward_spins": "1",
    "admin_broadcast": "",
    "min_withdraw_usdt": "5.0",
    "daily_rewards_ladder": "80,80,200,90,90,90,6000",
}

# Fallback used only if the DB row is somehow missing.
GEMS_PER_USDT_FALLBACK = 1000

DAILY_REWARD_LADDER = [80, 80, 200, 90, 90, 90, 6000]

DEFAULT_WHEEL_SEGMENTS = [
    ("50",    50,    "#12B886", 1),
    ("700",   700,   "#9B6BFF", 1),
    ("100",   100,   "#0EA579", 1),
    ("20",    20,    "#7C4DE0", 1),
    ("250",   250,   "#F2B705", 1),
    ("400",   400,   "#D9A203", 1),
    ("80",    80,    "#E85D75", 1),
    ("30",    30,    "#C94560", 1),
]

DEFAULT_TASKS = [
    ("Join Official Telegram Channel", "Join @chanelone13 to get updates and earn gems", 250, "telegram", "https://t.me/chanelone13", 0),
    ("Follow Us on X (Twitter)", "Follow our official X handle", 150, "social", "https://x.com", 1),
]


def _gen_code(nbytes=4) -> str:
    return secrets.token_hex(nbytes)


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.executescript(SCHEMA)
        # Database migrations
        try:
            await db.execute("ALTER TABLE gift_codes ADD COLUMN reward_usdt REAL NOT NULL DEFAULT 0.0")
            await db.commit()
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE users ADD COLUMN device_fingerprint TEXT")
            await db.commit()
        except Exception:
            pass
        await db.execute(
            "INSERT OR IGNORE INTO spin_config (id, min_reward, max_reward, max_spins_per_day) VALUES (1, 10, 700, 8)"
        )
        cur = await db.execute("SELECT COUNT(*) AS c FROM wheel_segments")
        row = await cur.fetchone()
        if row[0] == 0:
            for i, (label, value, color, is_real) in enumerate(DEFAULT_WHEEL_SEGMENTS):
                await db.execute(
                    "INSERT INTO wheel_segments (label, value_gems, color, is_real, sort_order) VALUES (?, ?, ?, ?, ?)",
                    (label, value, color, is_real, i),
                )
        for k, v in DEFAULT_SETTINGS.items():
            await db.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)", (k, v))
        
        cur_tasks = await db.execute("SELECT COUNT(*) AS c FROM tasks")
        t_row = await cur_tasks.fetchone()
        if t_row and t_row[0] == 0:
            for title, desc, reward, t_type, link, sort_order in DEFAULT_TASKS:
                await db.execute(
                    "INSERT INTO tasks (title, description, reward_gems, task_type, link, active, sort_order) VALUES (?, ?, ?, ?, ?, 1, ?)",
                    (title, desc, reward, t_type, link, sort_order),
                )

        await db.commit()


@asynccontextmanager
async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()


async def get_or_create_user(telegram_id: int, username, first_name, photo_url,
                              referred_by_code: str | None = None,
                              device_fingerprint: str | None = None):
    async with get_db() as db:
        referred_by_id = None
        if referred_by_code:
            cur = await db.execute("SELECT id FROM users WHERE referral_code = ?", (referred_by_code,))
            r = await cur.fetchone()
            if r:
                referred_by_id = r["id"]

        cur = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = await cur.fetchone()

        now = int(time.time())
        if row:
            if device_fingerprint and not row["device_fingerprint"]:
                await db.execute("UPDATE users SET device_fingerprint = ? WHERE id = ?", (device_fingerprint, row["id"]))
                await db.commit()

            if not row["referred_by"] and referred_by_id and referred_by_id != row["id"]:
                await db.execute(
                    "UPDATE users SET referred_by = ? WHERE id = ?",
                    (referred_by_id, row["id"]),
                )
                cur_check = await db.execute("SELECT 1 FROM referrals WHERE referred_id = ?", (row["id"],))
                if not await cur_check.fetchone():
                    cur_reward = await db.execute("SELECT value FROM app_settings WHERE key = 'referral_reward_gems'")
                    reward_gems_row = await cur_reward.fetchone()
                    ref_gems = int(reward_gems_row["value"]) if reward_gems_row else 250

                    cur_spins = await db.execute("SELECT value FROM app_settings WHERE key = 'referral_reward_spins'")
                    reward_spins_row = await cur_spins.fetchone()
                    ref_spins = int(reward_spins_row["value"]) if reward_spins_row else 1

                    await db.execute(
                        "INSERT INTO referrals (referrer_id, referred_id, bonus_gems, created_at) VALUES (?, ?, ?, ?)",
                        (referred_by_id, row["id"], ref_gems, now),
                    )
                    await db.execute(
                        "UPDATE users SET gems = gems + ?, bonus_spins = bonus_spins + ? WHERE id = ?",
                        (ref_gems, ref_spins, referred_by_id),
                    )
                await db.commit()
                cur = await db.execute("SELECT * FROM users WHERE id = ?", (row["id"],))
                row = await cur.fetchone()
            return row

        # Loop to ensure referral code is unique and avoid IntegrityError collisions
        code = _gen_code()
        for _ in range(5):
            cur = await db.execute("SELECT 1 FROM users WHERE referral_code = ?", (code,))
            if not await cur.fetchone():
                break
            code = _gen_code()

        cur = await db.execute(
            """INSERT INTO users (telegram_id, username, first_name, photo_url,
                                   referral_code, referred_by, created_at, device_fingerprint)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (telegram_id, username, first_name, photo_url, code, referred_by_id if (referred_by_id != cur.lastrowid) else None, now, device_fingerprint),
        )
        new_user_id = cur.lastrowid
        await db.commit()

        if referred_by_id and referred_by_id != new_user_id:
            cur_reward = await db.execute("SELECT value FROM app_settings WHERE key = 'referral_reward_gems'")
            reward_gems_row = await cur_reward.fetchone()
            ref_gems = int(reward_gems_row["value"]) if reward_gems_row else 250

            cur_spins = await db.execute("SELECT value FROM app_settings WHERE key = 'referral_reward_spins'")
            reward_spins_row = await cur_spins.fetchone()
            ref_spins = int(reward_spins_row["value"]) if reward_spins_row else 1

            cur_check = await db.execute("SELECT 1 FROM referrals WHERE referred_id = ?", (new_user_id,))
            if not await cur_check.fetchone():
                await db.execute(
                    "INSERT INTO referrals (referrer_id, referred_id, bonus_gems, created_at) VALUES (?, ?, ?, ?)",
                    (referred_by_id, new_user_id, ref_gems, now),
                )
                await db.execute(
                    "UPDATE users SET gems = gems + ?, bonus_spins = bonus_spins + ? WHERE id = ?",
                    (ref_gems, ref_spins, referred_by_id),
                )
                await db.commit()

        cur = await db.execute("SELECT * FROM users WHERE id = ?", (new_user_id,))
        return await cur.fetchone()


async def is_admin_key_valid(db, api_key: str) -> bool:
    cur = await db.execute("SELECT 1 FROM admins WHERE api_key = ?", (api_key,))
    return (await cur.fetchone()) is not None


async def get_all_settings(db) -> dict:
    cur = await db.execute("SELECT key, value FROM app_settings")
    rows = await cur.fetchall()
    settings = {**DEFAULT_SETTINGS, **{r["key"]: r["value"] for r in rows}}
    return settings


async def get_setting(db, key: str, default: str = "") -> str:
    cur = await db.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
    row = await cur.fetchone()
    return row["value"] if row else default
