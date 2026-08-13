"""
aiosqlite database layer.

Schema overview
----------------
users            one row per Telegram user
transactions     immutable ledger of every balance change (ads, tasks, referral, withdrawal, admin adjustment)
tasks            admin-configurable custom tasks (join channel, visit link, etc.)
user_tasks       which users completed which tasks
withdrawals      withdrawal requests + status tracking
ad_events        one row per verified Adsgram reward, used for daily-limit / cooldown checks
referrals        referral edges (referrer -> referred) + commission paid
"""
import aiosqlite
import contextlib
from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id       INTEGER PRIMARY KEY,
    username          TEXT,
    first_name        TEXT,
    balance           REAL NOT NULL DEFAULT 0,       -- current withdrawable USDT balance
    total_earned      REAL NOT NULL DEFAULT 0,       -- lifetime earnings, never decreases
    streak_count      INTEGER NOT NULL DEFAULT 0,
    last_checkin_date TEXT,                          -- 'YYYY-MM-DD' in UTC
    referred_by       INTEGER,                       -- telegram_id of referrer, NULL if none
    binance_pay_id    TEXT,                           -- last-used payout ID, prefilled on wallet page
    photo_file_path   TEXT,                            -- cached Telegram file_path for the profile photo (never a full URL — see bot.fetch_avatar_file_path)
    photo_synced_at   TEXT,                            -- when photo_file_path was last refreshed, for cache TTL
    is_banned         INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (referred_by) REFERENCES users(telegram_id)
);

CREATE TABLE IF NOT EXISTS transactions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id   INTEGER NOT NULL,
    type          TEXT NOT NULL,      -- 'ad_reward' | 'task_reward' | 'referral_commission' | 'referral_bonus' | 'signup_bonus' | 'spin_reward' | 'scratch_reward' | 'checkin' | 'withdrawal' | 'admin_adjust'
    amount        REAL NOT NULL,      -- positive = credit, negative = debit
    balance_after REAL NOT NULL,
    meta          TEXT,               -- JSON string, e.g. {"ad_block_id": "..."} for audit trail
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    description TEXT,
    url         TEXT NOT NULL,        -- link the user must visit / channel to join
    reward      REAL NOT NULL,
    task_type   TEXT NOT NULL DEFAULT 'link',  -- 'link' | 'telegram_join'
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id  INTEGER NOT NULL,
    task_id      INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'completed',  -- 'completed' | 'pending_review'
    completed_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(telegram_id, task_id),
    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id    INTEGER NOT NULL,
    amount         REAL NOT NULL,
    method         TEXT NOT NULL DEFAULT 'binance_pay', -- 'binance_pay' | 'usdt_address'
    payout_id      TEXT NOT NULL,      -- Binance Pay ID or wallet address
    network        TEXT,               -- e.g. 'TRC20', 'BEP20' — only used when method = usdt_address
    status         TEXT NOT NULL DEFAULT 'pending',   -- 'pending' | 'approved' | 'rejected'
    admin_note     TEXT,
    requested_at   TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at    TEXT,
    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
);

CREATE TABLE IF NOT EXISTS ad_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id  INTEGER NOT NULL,
    reward_event TEXT,                 -- Adsgram's event id, used to reject duplicate postbacks
    amount       REAL NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(reward_event),
    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
);

CREATE TABLE IF NOT EXISTS game_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id  INTEGER NOT NULL,
    game_type    TEXT NOT NULL,      -- 'spin' | 'scratch'
    amount       REAL NOT NULL,
    used_ad      INTEGER NOT NULL DEFAULT 0,  -- 1 if this play was unlocked by watching an ad
    meta         TEXT,               -- JSON string, e.g. {"landed_index": 3}
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
);

CREATE TABLE IF NOT EXISTS game_ad_unlocks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id  INTEGER NOT NULL,
    game_type    TEXT NOT NULL,
    reward_event TEXT NOT NULL,      -- Adsgram event id that unlocked this extra play
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(game_type, reward_event),
    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
);

CREATE TABLE IF NOT EXISTS referrals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id      INTEGER NOT NULL,
    referred_id      INTEGER NOT NULL UNIQUE,
    total_commission REAL NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (referrer_id) REFERENCES users(telegram_id),
    FOREIGN KEY (referred_id) REFERENCES users(telegram_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(telegram_id);
CREATE INDEX IF NOT EXISTS idx_withdrawals_user ON withdrawals(telegram_id);
CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status);
CREATE INDEX IF NOT EXISTS idx_ad_events_user_date ON ad_events(telegram_id, created_at);
CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);
CREATE INDEX IF NOT EXISTS idx_game_events_user_date ON game_events(telegram_id, game_type, created_at);
"""


# Settings that live in the DB (admin-editable at runtime) instead of only in env vars.
# Seeded from config.py defaults the first time the app boots; after that, the DB row wins.
DEFAULT_SETTINGS = {
    "ads_enabled": "1",
    "adsgram_block_id": settings.ADSGRAM_BLOCK_ID,
    "ad_reward_usdt": str(settings.AD_REWARD_USDT),
    "ad_daily_limit": str(settings.AD_DAILY_LIMIT),
    "ad_cooldown_seconds": str(settings.AD_COOLDOWN_SECONDS),
    "referral_commission_percent": str(settings.REFERRAL_COMMISSION_PERCENT),
    "referral_signup_bonus": str(settings.REFERRAL_SIGNUP_BONUS),
    "referral_fixed_reward": str(settings.REFERRAL_SIGNUP_BONUS),
    "min_withdrawal_usdt": str(settings.MIN_WITHDRAWAL_USDT),
    "withdrawal_tiers": ",".join(str(t) for t in settings.WITHDRAWAL_TIERS),
    "streak_rewards": ",".join(str(r) for r in settings.STREAK_REWARDS),
    "daily_checkin_enabled": "1",
    "support_username": "",
    "maintenance_mode": "0",
    "maintenance_message": "We'll be back shortly — thanks for your patience!",
    "adsgram_debug": "1" if settings.ADSGRAM_DEBUG else "0",

    # Spin Wheel — reward paid is always exactly the landed segment's number,
    # and the wheel can only land on segments inside [spin_min_reward, spin_max_reward]
    "spin_enabled": "1" if settings.SPIN_ENABLED else "0",
    "spin_min_reward": str(settings.SPIN_MIN_REWARD),
    "spin_max_reward": str(settings.SPIN_MAX_REWARD),
    "spin_segments": ",".join(str(x) for x in settings.SPIN_SEGMENTS),
    "spin_daily_free_spins": str(settings.SPIN_DAILY_FREE_SPINS),
    "spin_max_daily_spins": str(settings.SPIN_MAX_DAILY_SPINS),
    "spin_require_ad_after_free": "1" if settings.SPIN_REQUIRE_AD_AFTER_FREE else "0",
    "spin_cooldown_seconds": str(settings.SPIN_COOLDOWN_SECONDS),

    # Scratch Card
    "scratch_enabled": "1" if settings.SCRATCH_ENABLED else "0",
    "scratch_min_reward": str(settings.SCRATCH_MIN_REWARD),
    "scratch_max_reward": str(settings.SCRATCH_MAX_REWARD),
    "scratch_daily_free": str(settings.SCRATCH_DAILY_FREE),
    "scratch_max_daily": str(settings.SCRATCH_MAX_DAILY),
    "scratch_require_ad_after_free": "1" if settings.SCRATCH_REQUIRE_AD_AFTER_FREE else "0",
    "scratch_winning_cells": "3",
}


async def init_db():
    """Run once on app startup. Creates tables if they don't exist yet — safe to re-run."""
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.executescript(SCHEMA)

        # Migration: add columns introduced after the initial CREATE TABLE, for
        # databases that already existed before this version. ALTER TABLE ADD
        # COLUMN has no "IF NOT EXISTS" in SQLite, so check first and ignore if
        # the column is already there.
        cursor = await db.execute("PRAGMA table_info(users)")
        existing_columns = {row[1] for row in await cursor.fetchall()}
        for column, ddl_type in (("photo_file_path", "TEXT"), ("photo_synced_at", "TEXT")):
            if column not in existing_columns:
                await db.execute(f"ALTER TABLE users ADD COLUMN {column} {ddl_type}")

        # Seed any settings keys that don't exist yet (won't overwrite admin-edited values)
        for key, value in DEFAULT_SETTINGS.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
            )
        await db.commit()


async def get_settings(db) -> dict:
    """Returns all runtime settings as a dict of native-typed values."""
    cursor = await db.execute("SELECT key, value FROM settings")
    raw = {row["key"]: row["value"] for row in await cursor.fetchall()}

    def _f(key, default=0.0):
        try:
            return float(raw.get(key, default))
        except (TypeError, ValueError):
            return default

    def _i(key, default=0):
        try:
            return int(float(raw.get(key, default)))
        except (TypeError, ValueError):
            return default

    def _b(key, default=False):
        return raw.get(key, "1" if default else "0") == "1"

    def _list_f(key, default):
        val = raw.get(key)
        if not val:
            return default
        try:
            return [float(x) for x in val.split(",") if x.strip() != ""]
        except ValueError:
            return default

    return {
        "ads_enabled": _b("ads_enabled", True),
        "adsgram_block_id": raw.get("adsgram_block_id", ""),
        "ad_reward_usdt": _f("ad_reward_usdt", settings.AD_REWARD_USDT),
        "ad_daily_limit": _i("ad_daily_limit", settings.AD_DAILY_LIMIT),
        "ad_cooldown_seconds": _i("ad_cooldown_seconds", settings.AD_COOLDOWN_SECONDS),
        "referral_commission_percent": _f("referral_commission_percent", settings.REFERRAL_COMMISSION_PERCENT),
        "referral_signup_bonus": _f("referral_signup_bonus", settings.REFERRAL_SIGNUP_BONUS),
        "referral_fixed_reward": _f("referral_fixed_reward", settings.REFERRAL_SIGNUP_BONUS),
        "min_withdrawal_usdt": _f("min_withdrawal_usdt", settings.MIN_WITHDRAWAL_USDT),
        "withdrawal_tiers": _list_f("withdrawal_tiers", settings.WITHDRAWAL_TIERS),
        "streak_rewards": _list_f("streak_rewards", settings.STREAK_REWARDS),
        "daily_checkin_enabled": _b("daily_checkin_enabled", True),
        "support_username": raw.get("support_username", ""),
        "maintenance_mode": _b("maintenance_mode", False),
        "maintenance_message": raw.get("maintenance_message", ""),
        "adsgram_debug": _b("adsgram_debug", settings.ADSGRAM_DEBUG),

        "spin_enabled": _b("spin_enabled", settings.SPIN_ENABLED),
        "spin_min_reward": _f("spin_min_reward", settings.SPIN_MIN_REWARD),
        "spin_max_reward": _f("spin_max_reward", settings.SPIN_MAX_REWARD),
        "spin_segments": _list_f("spin_segments", settings.SPIN_SEGMENTS),
        "spin_daily_free_spins": _i("spin_daily_free_spins", settings.SPIN_DAILY_FREE_SPINS),
        "spin_max_daily_spins": _i("spin_max_daily_spins", settings.SPIN_MAX_DAILY_SPINS),
        "spin_require_ad_after_free": _b("spin_require_ad_after_free", settings.SPIN_REQUIRE_AD_AFTER_FREE),
        "spin_cooldown_seconds": _i("spin_cooldown_seconds", settings.SPIN_COOLDOWN_SECONDS),

        "scratch_enabled": _b("scratch_enabled", settings.SCRATCH_ENABLED),
        "scratch_min_reward": _f("scratch_min_reward", settings.SCRATCH_MIN_REWARD),
        "scratch_max_reward": _f("scratch_max_reward", settings.SCRATCH_MAX_REWARD),
        "scratch_daily_free": _i("scratch_daily_free", settings.SCRATCH_DAILY_FREE),
        "scratch_max_daily": _i("scratch_max_daily", settings.SCRATCH_MAX_DAILY),
        "scratch_require_ad_after_free": _b("scratch_require_ad_after_free", settings.SCRATCH_REQUIRE_AD_AFTER_FREE),
        "scratch_winning_cells": max(1, min(9, _i("scratch_winning_cells", 3))),
    }


@contextlib.asynccontextmanager
async def get_db():
    """
    Usage:
        async with get_db() as db:
            await db.execute(...)
            await db.commit()
    row_factory is set so results behave like dicts (row["column_name"]).
    """
    db = await aiosqlite.connect(settings.DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()
