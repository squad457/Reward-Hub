"""
main.py
FastAPI backend for the Reward Hub Telegram Mini App.

Run locally:
    uvicorn main:app --reload --port 8000

Env vars required:
    BOT_TOKEN     - Telegram bot token, used to validate WebApp initData
    ADMIN_KEY     - bootstrap admin API key (inserted into `admins` table on startup)
"""

import hashlib
import hmac
import json
import os
import random
import time
import asyncio
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import database as db
from bot import bot, dp

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_KEY_BOOTSTRAP = os.environ.get("ADMIN_KEY", "")

app = FastAPI(title="Reward Hub API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # tighten to your Vercel domain in production
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup():
    await db.init_db()
    if ADMIN_KEY_BOOTSTRAP:
        async with db.get_db() as conn:
            await conn.execute("INSERT OR IGNORE INTO admins (api_key) VALUES (?)", (ADMIN_KEY_BOOTSTRAP,))
            await conn.commit()
    # Start the Telegram Bot in the background under the same asyncio loop
    asyncio.create_task(dp.start_polling(bot))


@app.on_event("shutdown")
async def _shutdown():
    # Gracefully close bot session
    await bot.session.close()


# ---------------------------------------------------------------- auth ----

def verify_telegram_init_data(init_data: str) -> dict:
    if not BOT_TOKEN:
        raise HTTPException(500, "BOT_TOKEN not configured")

    parsed = dict(parse_qsl(init_data, strict_parsing=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Missing hash in initData")

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise HTTPException(401, "Invalid initData signature")

    auth_date = int(parsed.get("auth_date", 0))
    if time.time() - auth_date > 86400:
        raise HTTPException(401, "initData expired")

    return json.loads(parsed.get("user", "{}"))


async def current_user(x_init_data: str = Header(...), x_ref_code: str | None = Header(default=None)):
    tg_user = verify_telegram_init_data(x_init_data)
    if not tg_user.get("id"):
        raise HTTPException(401, "No user in initData")
    row = await db.get_or_create_user(
        telegram_id=tg_user["id"],
        username=tg_user.get("username"),
        first_name=tg_user.get("first_name"),
        photo_url=tg_user.get("photo_url"),
        referred_by_code=x_ref_code,
    )
    return row


async def require_admin(x_admin_key: str = Header(...)):
    async with db.get_db() as conn:
        if not await db.is_admin_key_valid(conn, x_admin_key):
            raise HTTPException(403, "Invalid admin key")
    return x_admin_key


def user_public(row) -> dict:
    now = int(time.time())
    vip_active = bool(row["is_vip"]) and (not row["vip_expires_at"] or row["vip_expires_at"] > now)
    return {
        "id": row["id"],
        "telegram_id": row["telegram_id"],
        "first_name": row["first_name"],
        "username": row["username"],
        "photo_url": row["photo_url"],
        "gems": row["gems"],
        "balance_usdt": row["balance_usdt"],
        "daily_streak": row["daily_streak"],
        "referral_code": row["referral_code"],
        "bonus_spins": row["bonus_spins"],
        "is_vip": vip_active,
    }


# -------------------------------------------------------------- /me ----

@app.get("/api/me")
async def get_me(user=Depends(current_user)):
    return user_public(user)


# ------------------------------------------------------------- settings --
# Public, read-only subset the frontend needs at boot (no auth required —
# it's config, not user data). Everything here is edited from /api/admin/settings.

@app.get("/api/settings")
async def public_settings():
    async with db.get_db() as conn:
        s = await db.get_all_settings(conn)
    return {
        "app_name": s["app_name"],
        "adsgram_block_id": s["adsgram_block_id"],
        "bot_username": s["bot_username"],
        "gems_per_usdt": s["gems_per_usdt"],
        "referral_reward_gems": s["referral_reward_gems"],
        "admin_broadcast": s["admin_broadcast"],
        "min_withdraw_usdt": s["min_withdraw_usdt"],
    }


# ------------------------------------------------------- daily rewards ----

@app.post("/api/daily-reward/claim")
async def claim_daily_reward(user=Depends(current_user)):
    now = int(time.time())
    last = user["last_daily_claim"] or 0
    seconds_since = now - last

    if seconds_since < 86400:
        raise HTTPException(400, "Already claimed today's reward")

    streak = user["daily_streak"]
    streak = (streak + 1) if (last and seconds_since < 172800) else 1
    day_index = min(streak - 1, len(db.DAILY_REWARD_LADDER) - 1)
    reward = db.DAILY_REWARD_LADDER[day_index]

    async with db.get_db() as conn:
        await conn.execute(
            "UPDATE users SET gems = gems + ?, daily_streak = ?, last_daily_claim = ? WHERE id = ?",
            (reward, streak, now, user["id"]),
        )
        await conn.commit()

    return {"reward_gems": reward, "new_streak": streak}


@app.get("/api/daily-reward/status")
async def daily_reward_status(user=Depends(current_user)):
    now = int(time.time())
    last = user["last_daily_claim"] or 0
    return {
        "claimable": (now - last) >= 86400,
        "current_streak": user["daily_streak"],
        "ladder": db.DAILY_REWARD_LADDER,
    }


# ------------------------------------------------------------ spin wheel --
#
# The wheel can show segments with numbers outside the admin's min/max range
# (kept for visual excitement) but the random pick is drawn ONLY from segments
# flagged is_real=1, whose value already sits inside [min_reward, max_reward].
# The angle the wheel animates to is derived from the chosen segment's index,
# so the frontend always visually lands exactly where the payout says it does.

async def _get_spin_config(conn):
    cur = await conn.execute("SELECT * FROM spin_config WHERE id = 1")
    return await cur.fetchone()


async def _get_wheel_segments(conn):
    cur = await conn.execute("SELECT * FROM wheel_segments ORDER BY sort_order")
    return await cur.fetchall()


@app.get("/api/spin/wheel")
async def get_wheel(user=Depends(current_user)):
    async with db.get_db() as conn:
        segments = await _get_wheel_segments(conn)
        cfg = await _get_spin_config(conn)
    return {
        "segments": [
            {"id": s["id"], "label": s["label"], "value_gems": s["value_gems"], "color": s["color"]}
            for s in segments
        ],
        "max_spins_per_day": cfg["max_spins_per_day"],
    }


@app.post("/api/spin")
async def spin_wheel(user=Depends(current_user)):
    now = int(time.time())
    async with db.get_db() as conn:
        cfg = await _get_spin_config(conn)
        segments = await _get_wheel_segments(conn)

        real_segments = [s for s in segments if s["is_real"] and cfg["min_reward"] <= s["value_gems"] <= cfg["max_reward"]]
        if not real_segments:
            raise HTTPException(500, "No eligible wheel segments configured")

        reset_at = user["spins_reset_at"] or 0
        spins_today = user["spins_today"]
        if now >= reset_at:
            spins_today = 0
            reset_at = now + 86400

        allowance = cfg["max_spins_per_day"] + user["bonus_spins"]
        if spins_today >= allowance:
            raise HTTPException(400, f"Daily spin limit reached ({allowance}/day)")

        chosen = random.choice(real_segments)
        bonus_spin_used = spins_today >= cfg["max_spins_per_day"]

        await conn.execute(
            """UPDATE users SET gems = gems + ?, spins_today = ?, spins_reset_at = ?,
                                 bonus_spins = bonus_spins - ? WHERE id = ?""",
            (chosen["value_gems"], spins_today + 1, reset_at, 1 if bonus_spin_used else 0, user["id"]),
        )
        await conn.execute(
            "INSERT INTO spin_history (user_id, reward_gems, segment_id, created_at) VALUES (?, ?, ?, ?)",
            (user["id"], chosen["value_gems"], chosen["id"], now),
        )
        await conn.commit()

        all_segments = await _get_wheel_segments(conn)

    segment_index = next(i for i, s in enumerate(all_segments) if s["id"] == chosen["id"])
    return {
        "reward_gems": chosen["value_gems"],
        "segment_id": chosen["id"],
        "segment_index": segment_index,
        "total_segments": len(all_segments),
        "spins_left": allowance - (spins_today + 1),
    }


# ----------------------------------------------------------------- tasks --

@app.get("/api/tasks")
async def list_tasks(user=Depends(current_user)):
    async with db.get_db() as conn:
        cur = await conn.execute(
            """SELECT t.*, ut.status
               FROM tasks t
               LEFT JOIN user_tasks ut ON ut.task_id = t.id AND ut.user_id = ?
               WHERE t.active = 1
               ORDER BY t.sort_order""",
            (user["id"],),
        )
        rows = await cur.fetchall()

    return [
        {
            "id": r["id"], "title": r["title"], "description": r["description"],
            "reward_gems": r["reward_gems"], "task_type": r["task_type"],
            "link": r["link"], "status": r["status"] or "pending",
        }
        for r in rows
    ]


class ClaimTaskBody(BaseModel):
    task_id: int


@app.post("/api/tasks/claim")
async def claim_task(body: ClaimTaskBody, user=Depends(current_user)):
    async with db.get_db() as conn:
        cur = await conn.execute("SELECT * FROM tasks WHERE id = ? AND active = 1", (body.task_id,))
        task = await cur.fetchone()
        if not task:
            raise HTTPException(404, "Task not found")

        cur = await conn.execute(
            "SELECT status FROM user_tasks WHERE user_id = ? AND task_id = ?", (user["id"], body.task_id)
        )
        existing = await cur.fetchone()
        if existing and existing["status"] == "claimed":
            raise HTTPException(400, "Task already claimed")

        now = int(time.time())
        await conn.execute(
            """INSERT INTO user_tasks (user_id, task_id, status, claimed_at) VALUES (?, ?, 'claimed', ?)
               ON CONFLICT(user_id, task_id) DO UPDATE SET status='claimed', claimed_at=excluded.claimed_at""",
            (user["id"], body.task_id, now),
        )
        await conn.execute("UPDATE users SET gems = gems + ? WHERE id = ?", (task["reward_gems"], user["id"]))
        await conn.commit()

    return {"reward_gems": task["reward_gems"]}


class AdRewardBody(BaseModel):
    placement: str  # "daily_bonus" | "task" | "spin_bonus"


@app.post("/api/ads/reward")
async def ad_reward(body: AdRewardBody, user=Depends(current_user)):
    """Called after an AdsGram rewarded ad finishes successfully (frontend verifies completion)."""
    grants = {"daily_bonus": 40, "spin_bonus": 0, "task": 0}
    gems = grants.get(body.placement, 0)
    bonus_spin = 1 if body.placement == "spin_bonus" else 0

    async with db.get_db() as conn:
        await conn.execute(
            "UPDATE users SET gems = gems + ?, bonus_spins = bonus_spins + ? WHERE id = ?",
            (gems, bonus_spin, user["id"]),
        )
        await conn.commit()

    return {"reward_gems": gems, "bonus_spins": bonus_spin}


# -------------------------------------------------------------- referral --

@app.get("/api/referral")
async def referral_info(user=Depends(current_user)):
    async with db.get_db() as conn:
        cur = await conn.execute("SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ?", (user["id"],))
        count = (await cur.fetchone())["c"]
    return {"referral_code": user["referral_code"], "invited_count": count}


# ------------------------------------------------------------- gift codes --

class RedeemBody(BaseModel):
    code: str


@app.post("/api/gift-code/redeem")
async def redeem_gift_code(body: RedeemBody, user=Depends(current_user)):
    now = int(time.time())
    async with db.get_db() as conn:
        cur = await conn.execute("SELECT * FROM gift_codes WHERE code = ? AND active = 1", (body.code.strip(),))
        gc = await cur.fetchone()
        if not gc:
            raise HTTPException(404, "Invalid gift code")
        if gc["expires_at"] and gc["expires_at"] < now:
            raise HTTPException(400, "Gift code expired")
        if gc["used_count"] >= gc["max_uses"]:
            raise HTTPException(400, "Gift code fully redeemed")

        cur = await conn.execute(
            "SELECT 1 FROM gift_code_redemptions WHERE user_id = ? AND gift_code_id = ?", (user["id"], gc["id"])
        )
        if await cur.fetchone():
            raise HTTPException(400, "You already used this code")

        await conn.execute(
            "UPDATE users SET gems = gems + ?, bonus_spins = bonus_spins + ?, balance_usdt = balance_usdt + ? WHERE id = ?",
            (gc["reward_gems"], gc["reward_spins"], gc["reward_usdt"], user["id"]),
        )
        await conn.execute("UPDATE gift_codes SET used_count = used_count + 1 WHERE id = ?", (gc["id"],))
        await conn.execute(
            "INSERT INTO gift_code_redemptions (user_id, gift_code_id, redeemed_at) VALUES (?, ?, ?)",
            (user["id"], gc["id"], now),
        )
        await conn.commit()

    return {"reward_gems": gc["reward_gems"], "reward_spins": gc["reward_spins"], "reward_usdt": gc["reward_usdt"]}


# ------------------------------------------------------------- withdrawal --

class WithdrawBody(BaseModel):
    amount_usdt: float
    method: str        # binance_pay | usdt_address
    destination: str


@app.post("/api/withdraw")
async def request_withdrawal(body: WithdrawBody, user=Depends(current_user)):
    if body.amount_usdt <= 0:
        raise HTTPException(400, "Invalid amount")

    async with db.get_db() as conn:
        min_wd = float(await db.get_setting(conn, "min_withdraw_usdt", "5.0"))
        if body.amount_usdt < min_wd:
            raise HTTPException(400, f"Minimum withdrawal amount is ${min_wd:.2f} USDT")

    now = int(time.time())
    async with db.get_db() as conn:
        # Atomically deduct from balance only if user has sufficient USDT
        cur = await conn.execute(
            "UPDATE users SET balance_usdt = balance_usdt - ? WHERE id = ? AND balance_usdt >= ?",
            (body.amount_usdt, user["id"], body.amount_usdt)
        )
        if cur.rowcount == 0:
            raise HTTPException(400, "Insufficient balance")

        await conn.execute(
            """INSERT INTO withdrawals (user_id, amount_usdt, method, destination, status, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            (user["id"], body.amount_usdt, body.method, body.destination, now),
        )
        await conn.commit()
    return {"status": "pending"}


@app.post("/api/gems/convert")
async def convert_gems_to_usdt(user=Depends(current_user)):
    """Convert all convertible gems into USDT balance at the admin-configured rate."""
    async with db.get_db() as conn:
        # Fetch current gems inside transaction to prevent concurrent modification
        cur = await conn.execute("SELECT gems FROM users WHERE id = ?", (user["id"],))
        row = await cur.fetchone()
        current_gems = row["gems"] if row else 0
        if current_gems <= 0:
            raise HTTPException(400, "Nothing to convert")

        rate = float(await db.get_setting(conn, "gems_per_usdt", str(db.GEMS_PER_USDT_FALLBACK)))
        usdt = current_gems / rate

        # Atomically zero out gems and credit USDT, verifying gems hasn't changed since read
        cur = await conn.execute(
            "UPDATE users SET gems = 0, balance_usdt = balance_usdt + ? WHERE id = ? AND gems = ?",
            (usdt, user["id"], current_gems)
        )
        if cur.rowcount == 0:
            raise HTTPException(400, "Conversion failed due to concurrent update. Please try again.")
        await conn.commit()
    return {"converted_usdt": usdt}


# ==================================================================== #
#  ADMIN — guarded by X-Admin-Key header
# ==================================================================== #

class SettingsBody(BaseModel):
    adsgram_block_id: str | None = None
    bot_username: str | None = None
    gems_per_usdt: str | None = None
    app_name: str | None = None
    referral_reward_gems: str | None = None
    admin_broadcast: str | None = None
    min_withdraw_usdt: str | None = None


@app.get("/api/admin/settings")
async def admin_get_settings(_=Depends(require_admin)):
    async with db.get_db() as conn:
        return await db.get_all_settings(conn)


@app.post("/api/admin/settings")
async def admin_set_settings(body: SettingsBody, _=Depends(require_admin)):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No settings provided")
    async with db.get_db() as conn:
        for k, v in updates.items():
            await conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, v),
            )
        await conn.commit()
        settings = await db.get_all_settings(conn)
    return settings


@app.get("/api/admin/stats")
async def admin_stats(_=Depends(require_admin)):
    async with db.get_db() as conn:
        users = (await (await conn.execute("SELECT COUNT(*) c FROM users")).fetchone())["c"]
        pending_wd = (await (await conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(amount_usdt),0) s FROM withdrawals WHERE status='pending'"
        )).fetchone())
    return {"total_users": users, "pending_withdrawals": pending_wd["c"], "pending_withdrawal_usdt": pending_wd["s"]}


class SpinConfigBody(BaseModel):
    min_reward: int
    max_reward: int
    max_spins_per_day: int


@app.get("/api/admin/spin-config")
async def get_spin_config(_=Depends(require_admin)):
    async with db.get_db() as conn:
        cfg = await _get_spin_config(conn)
    return dict(cfg)


@app.post("/api/admin/spin-config")
async def set_spin_config(body: SpinConfigBody, _=Depends(require_admin)):
    if body.min_reward > body.max_reward:
        raise HTTPException(400, "min_reward cannot exceed max_reward")
    async with db.get_db() as conn:
        await conn.execute(
            "UPDATE spin_config SET min_reward=?, max_reward=?, max_spins_per_day=? WHERE id=1",
            (body.min_reward, body.max_reward, body.max_spins_per_day),
        )
        await conn.commit()
        segments = await _get_wheel_segments(conn)
        eligible = sum(1 for s in segments if s["is_real"] and body.min_reward <= s["value_gems"] <= body.max_reward)
    warning = None
    if eligible == 0:
        warning = "No wheel segments currently fall inside this range — spins will fail until you add one."
    return {"ok": True, "eligible_segments": eligible, "warning": warning}


class WheelSegmentBody(BaseModel):
    label: str
    value_gems: int
    color: str = "#12B886"
    is_real: bool = True
    sort_order: int = 0


@app.get("/api/admin/wheel-segments")
async def admin_list_segments(_=Depends(require_admin)):
    async with db.get_db() as conn:
        segments = await _get_wheel_segments(conn)
    return [dict(s) for s in segments]


@app.post("/api/admin/wheel-segments")
async def admin_add_segment(body: WheelSegmentBody, _=Depends(require_admin)):
    async with db.get_db() as conn:
        cur = await conn.execute(
            "INSERT INTO wheel_segments (label, value_gems, color, is_real, sort_order) VALUES (?, ?, ?, ?, ?)",
            (body.label, body.value_gems, body.color, int(body.is_real), body.sort_order),
        )
        await conn.commit()
    return {"id": cur.lastrowid}


@app.delete("/api/admin/wheel-segments/{segment_id}")
async def admin_delete_segment(segment_id: int, _=Depends(require_admin)):
    async with db.get_db() as conn:
        await conn.execute("DELETE FROM wheel_segments WHERE id = ?", (segment_id,))
        await conn.commit()
    return {"ok": True}


class TaskBody(BaseModel):
    title: str
    description: str | None = None
    reward_gems: int
    task_type: str = "special"
    link: str | None = None
    sort_order: int = 0


@app.post("/api/admin/tasks")
async def admin_create_task(body: TaskBody, _=Depends(require_admin)):
    async with db.get_db() as conn:
        cur = await conn.execute(
            """INSERT INTO tasks (title, description, reward_gems, task_type, link, sort_order)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (body.title, body.description, body.reward_gems, body.task_type, body.link, body.sort_order),
        )
        await conn.commit()
    return {"id": cur.lastrowid}


@app.delete("/api/admin/tasks/{task_id}")
async def admin_deactivate_task(task_id: int, _=Depends(require_admin)):
    async with db.get_db() as conn:
        await conn.execute("UPDATE tasks SET active = 0 WHERE id = ?", (task_id,))
        await conn.commit()
    return {"ok": True}


class GiftCodeBody(BaseModel):
    code: str
    reward_gems: int = 0
    reward_spins: int = 0
    reward_usdt: float = 0.0
    max_uses: int = 1
    expires_in_days: int | None = None


@app.post("/api/admin/gift-codes")
async def admin_create_gift_code(body: GiftCodeBody, _=Depends(require_admin)):
    expires_at = int(time.time()) + body.expires_in_days * 86400 if body.expires_in_days else None
    async with db.get_db() as conn:
        try:
            await conn.execute(
                """INSERT INTO gift_codes (code, reward_gems, reward_spins, reward_usdt, max_uses, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (body.code, body.reward_gems, body.reward_spins, body.reward_usdt, body.max_uses, expires_at),
            )
            await conn.commit()
        except Exception:
            raise HTTPException(400, "Code already exists")
    return {"ok": True}


@app.get("/api/admin/withdrawals")
async def admin_list_withdrawals(status: str = "pending", _=Depends(require_admin)):
    async with db.get_db() as conn:
        cur = await conn.execute(
            """SELECT w.*, u.username, u.telegram_id FROM withdrawals w
               JOIN users u ON u.id = w.user_id WHERE w.status = ? ORDER BY w.created_at""",
            (status,),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


class WithdrawalUpdateBody(BaseModel):
    status: str  # paid | rejected


@app.post("/api/admin/withdrawals/{withdrawal_id}")
async def admin_update_withdrawal(withdrawal_id: int, body: WithdrawalUpdateBody, _=Depends(require_admin)):
    if body.status not in ("paid", "rejected"):
        raise HTTPException(400, "status must be 'paid' or 'rejected'")
    async with db.get_db() as conn:
        cur = await conn.execute("SELECT * FROM withdrawals WHERE id = ?", (withdrawal_id,))
        wd = await cur.fetchone()
        if not wd:
            raise HTTPException(404, "Withdrawal not found")
        if body.status == "rejected" and wd["status"] == "pending":
            # refund the reserved balance back to the user
            await conn.execute("UPDATE users SET balance_usdt = balance_usdt + ? WHERE id = ?",
                                (wd["amount_usdt"], wd["user_id"]))
        await conn.execute("UPDATE withdrawals SET status = ? WHERE id = ?", (body.status, withdrawal_id))
        await conn.commit()
    return {"ok": True}
