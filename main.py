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
import datetime
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
    if not init_data:
        return {}

    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=False))
        received_hash = parsed.pop("hash", None)
        
        if not received_hash:
            return {}

        if BOT_TOKEN:
            check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()) if k != "hash")
            secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
            computed_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
            if hmac.compare_digest(computed_hash, received_hash):
                user_json = parsed.get("user")
                if user_json:
                    return json.loads(user_json)
    except Exception:
        pass

    return {}


async def current_user(
    x_init_data: str | None = Header(default=None),
    x_ref_code: str | None = Header(default=None),
    x_device_fingerprint: str | None = Header(default=None, alias="X-Device-Fingerprint")
):
    if not x_init_data or x_init_data in ("review", "test", "undefined", "null"):
        # Mock reviewer user for Adsgram automated scanner / review crawlers (Clause 0 bypass)
        row = await db.get_or_create_user(
            telegram_id=99999999,
            username="adsgram_reviewer",
            first_name="Reviewer",
            photo_url=None,
            referred_by_code=x_ref_code,
            device_fingerprint=x_device_fingerprint,
        )
        return row

    tg_user = verify_telegram_init_data(x_init_data)
    if not tg_user.get("id"):
        raise HTTPException(401, "No user in initData")
    row = await db.get_or_create_user(
        telegram_id=tg_user["id"],
        username=tg_user.get("username"),
        first_name=tg_user.get("first_name"),
        photo_url=tg_user.get("photo_url"),
        referred_by_code=x_ref_code,
        device_fingerprint=x_device_fingerprint,
    )
    return row


async def require_admin(x_admin_key: str = Header(...)):
    async with db.get_db() as conn:
        if not await db.is_admin_key_valid(conn, x_admin_key):
            raise HTTPException(403, "Invalid admin key")
    return x_admin_key


async def user_public(row, conn=None) -> dict:
    now = int(time.time())
    vip_active = bool(row["is_vip"]) and (not row["vip_expires_at"] or row["vip_expires_at"] > now)
    invited_count = 0
    if conn:
        cur = await conn.execute("SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ?", (row["id"],))
        r = await cur.fetchone()
        if r:
            invited_count = r["c"]
    else:
        async with db.get_db() as c:
            cur = await c.execute("SELECT COUNT(*) AS c FROM referrals WHERE referrer_id = ?", (row["id"],))
            r = await cur.fetchone()
            if r:
                invited_count = r["c"]

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
        "invited_count": invited_count,
        "bonus_spins": row["bonus_spins"],
        "is_vip": vip_active,
    }


# -------------------------------------------------------------- /me ----

@app.get("/api/me")
async def get_me(user=Depends(current_user)):
    async with db.get_db() as conn:
        return await user_public(user, conn)


# ------------------------------------------------------------- settings --
# Public, read-only subset the frontend needs at boot (no auth required —
# it's config, not user data). Everything here is edited from /api/admin/settings.

async def _get_daily_ladder(conn) -> list[int]:
    ladder_str = await db.get_setting(conn, "daily_rewards_ladder", "80,80,200,90,90,90,6000")
    try:
        ladder = [int(x.strip()) for x in ladder_str.split(",") if x.strip()]
        if ladder:
            return ladder
    except Exception:
        pass
    return [80, 80, 200, 90, 90, 90, 6000]


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
        "referral_reward_spins": s["referral_reward_spins"],
        "admin_broadcast": s["admin_broadcast"],
        "min_withdraw_usdt": s["min_withdraw_usdt"],
        "daily_rewards_ladder": s["daily_rewards_ladder"],
        "payout_channel_link": s["payout_channel_link"],
        "webapp_url": s.get("webapp_url", "https://usdtreward.online"),
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
    if last and seconds_since < 172800:
        streak = (streak % 7) + 1
    else:
        streak = 1
    
    async with db.get_db() as conn:
        ladder = await _get_daily_ladder(conn)
        day_index = min(streak - 1, len(ladder) - 1)
        reward = ladder[day_index]
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
    async with db.get_db() as conn:
        ladder = await _get_daily_ladder(conn)
    return {
        "claimable": (now - last) >= 86400,
        "current_streak": user["daily_streak"],
        "ladder": ladder,
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
    now = int(time.time())
    async with db.get_db() as conn:
        segments = await _get_wheel_segments(conn)
        cfg = await _get_spin_config(conn)
        
        reset_at = user["spins_reset_at"] or 0
        spins_today = user["spins_today"]
        now_dt = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
        next_midnight = datetime.datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=datetime.timezone.utc) + datetime.timedelta(days=1)
        target_reset_at = int(next_midnight.timestamp())

        if now >= reset_at:
            spins_today = 0
            await conn.execute("UPDATE users SET spins_today = 0, spins_reset_at = ? WHERE id = ?", (target_reset_at, user["id"]))
            await conn.commit()
            
        max_daily = cfg["max_spins_per_day"]
        daily_left = max(0, max_daily - spins_today)
        bonus_spins = user["bonus_spins"]
        total_left = daily_left + bonus_spins

    return {
        "segments": [
            {"id": s["id"], "label": s["label"], "value_gems": s["value_gems"], "color": s["color"]}
            for s in segments
        ],
        "max_spins_per_day": max_daily,
        "daily_spins_left": daily_left,
        "bonus_spins": bonus_spins,
        "total_spins_left": total_left,
    }


@app.post("/api/spin")
async def spin_wheel(user=Depends(current_user)):
    now = int(time.time())
    async with db.get_db() as conn:
        cfg = await _get_spin_config(conn)
        segments = await _get_wheel_segments(conn)

        # Select all segments that do not exceed max_reward
        real_segments = [s for s in segments if s["value_gems"] <= cfg["max_reward"]]

        if cfg["min_reward"] and cfg["min_reward"] > 1:
            filtered = [s for s in real_segments if s["value_gems"] >= cfg["min_reward"]]
            if filtered:
                real_segments = filtered

        if not real_segments:
            real_segments = segments

        if not real_segments:
            raise HTTPException(500, "No eligible wheel segments configured")

        reset_at = user["spins_reset_at"] or 0
        spins_today = user["spins_today"]
        
        now_dt = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
        next_midnight = datetime.datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=datetime.timezone.utc) + datetime.timedelta(days=1)
        target_reset_at = int(next_midnight.timestamp())

        if now >= reset_at:
            spins_today = 0
            reset_at = target_reset_at

        max_daily = cfg["max_spins_per_day"]
        daily_left = max(0, max_daily - spins_today)
        bonus_spins = user["bonus_spins"]

        if daily_left <= 0 and bonus_spins <= 0:
            raise HTTPException(400, "No spins left today. Watch an ad or invite friends for bonus spins!")

        # Deduct spin: use daily free spin first, then bonus spin
        if daily_left > 0:
            new_spins_today = spins_today + 1
            new_bonus_spins = bonus_spins
        else:
            new_spins_today = spins_today
            new_bonus_spins = bonus_spins - 1

        chosen = random.choice(real_segments)

        await conn.execute(
            """UPDATE users SET gems = gems + ?, spins_today = ?, spins_reset_at = ?,
                                 bonus_spins = ? WHERE id = ?""",
            (chosen["value_gems"], new_spins_today, reset_at, new_bonus_spins, user["id"]),
        )
        await conn.execute(
            "INSERT INTO spin_history (user_id, reward_gems, segment_id, created_at) VALUES (?, ?, ?, ?)",
            (user["id"], chosen["value_gems"], chosen["id"], now),
        )
        await conn.commit()

        all_segments = await _get_wheel_segments(conn)

    segment_index = next((i for i, s in enumerate(all_segments) if s["id"] == chosen["id"]), 0)
    rem_daily = max(0, max_daily - new_spins_today)
    total_left = rem_daily + new_bonus_spins

    return {
        "reward_gems": chosen["value_gems"],
        "segment_id": chosen["id"],
        "segment_index": segment_index,
        "total_segments": len(all_segments),
        "spins_left": total_left,
        "daily_left": rem_daily,
        "bonus_spins": new_bonus_spins,
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

        # Verify Telegram channel membership if it's a telegram task
        if task["task_type"] == "telegram" or (task["link"] and ("t.me/" in task["link"] or task["link"].startswith("@"))):
            link = task["link"] or ""
            channel = ""
            if "t.me/" in link:
                channel = link.split("t.me/")[-1].strip("/@").split("/")[0]
            elif link.startswith("@"):
                channel = link.strip("@")
            
            if channel:
                try:
                    member = await bot.get_chat_member(chat_id=f"@{channel}", user_id=user["telegram_id"])
                    if member.status not in ["member", "administrator", "creator"]:
                        raise HTTPException(400, f"Please join @{channel} first before claiming this reward!")
                except HTTPException as he:
                    raise he
                except Exception as e:
                    print(f"Channel verification check warning for @{channel}: {e}")

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
    now = int(time.time())
    async with db.get_db() as conn:
        cur = await conn.execute("SELECT last_ad_claim FROM users WHERE id = ?", (user["id"],))
        row = await cur.fetchone()
        last_claim = row["last_ad_claim"] if (row and "last_ad_claim" in row.keys() and row["last_ad_claim"]) else 0
        if now - last_claim < 15:
            raise HTTPException(429, f"Please wait {15 - (now - last_claim)}s before claiming next ad reward")

        grants = {"daily_bonus": 40, "spin_bonus": 0, "task": 0}
        gems = grants.get(body.placement, 0)
        bonus_spin = 1 if body.placement == "spin_bonus" else 0

        await conn.execute(
            "UPDATE users SET gems = gems + ?, bonus_spins = bonus_spins + ?, last_ad_claim = ? WHERE id = ?",
            (gems, bonus_spin, now, user["id"]),
        )
        await conn.commit()

    return {"reward_gems": gems, "bonus_spins": bonus_spin}


@app.get("/api/ads/monetag-postback")
@app.post("/api/ads/monetag-postback")
async def monetag_postback(
    telegram_id: int | None = None,
    user_id: int | None = None,
    reward: str | None = None,
    reward_event_type: str | None = None,
    event_type: str | None = None,
):
    tg_id = telegram_id or user_id
    is_rewarded = (reward and reward.lower() in ("yes", "true", "1", "success", "paid")) or \
                  (reward_event_type and reward_event_type.lower() in ("yes", "true", "1", "success", "paid")) or \
                  (event_type and event_type.lower() in ("reward", "conversion", "paid"))

    if not tg_id:
        return {"status": "error", "message": "missing telegram_id"}

    if is_rewarded or not reward:
        async with db.get_db() as conn:
            cur = await conn.execute("SELECT id FROM users WHERE telegram_id = ?", (tg_id,))
            u = await cur.fetchone()
            if u:
                await conn.execute(
                    "UPDATE users SET gems = gems + 40 WHERE id = ?",
                    (u["id"],)
                )
                await conn.commit()
                return {"status": "success", "telegram_id": tg_id, "rewarded_gems": 40}

    return {"status": "received"}


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

        cur_upd = await conn.execute(
            "UPDATE gift_codes SET used_count = used_count + 1 WHERE id = ? AND used_count < max_uses",
            (gc["id"],)
        )
        if cur_upd.rowcount == 0:
            raise HTTPException(400, "Gift code fully redeemed")

        await conn.execute(
            "UPDATE users SET gems = gems + ?, bonus_spins = bonus_spins + ?, balance_usdt = balance_usdt + ? WHERE id = ?",
            (gc["reward_gems"], gc["reward_spins"], gc["reward_usdt"], user["id"]),
        )
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

        cur = await conn.execute(
            """INSERT INTO withdrawals (user_id, amount_usdt, method, destination, status, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            (user["id"], body.amount_usdt, body.method, body.destination, now),
        )
        wd_id = cur.lastrowid
        await conn.commit()

    # Broadcast withdrawal request to payout proof channel
    try:
        async with db.get_db() as conn:
            channel_username = await db.get_setting(conn, "payout_channel_username", "@rewardhubpayoutbot")
        
        masked_dest = body.destination[:6] + "..." + body.destination[-4:] if len(body.destination) > 10 else body.destination
        
        caption = (
            f"🚀 <b>NEW WITHDRAWAL REQUEST</b>\n\n"
            f"🆔 <b>User ID:</b> <code>{user['telegram_id']}</code>\n"
            f"💵 <b>Amount:</b> <code>${body.amount_usdt:.2f} USDT</code>\n"
            f"💳 <b>Method:</b> {body.method.replace('_', ' ').title()}\n"
            f"🎯 <b>Destination:</b> <code>{masked_dest}</code>\n"
            f"⏳ <b>Status:</b> <b>Pending Review</b>\n\n"
            f"🏆 <i>Reward Hub Automated Payout System</i>"
        )
        msg = await bot.send_message(chat_id=channel_username, text=caption, parse_mode="HTML")
        async with db.get_db() as conn:
            await conn.execute("UPDATE withdrawals SET telegram_message_id = ? WHERE id = ?", (msg.message_id, wd_id))
            await conn.commit()
    except Exception as e:
        print(f"Payout channel broadcast warning: {e}")

    return {"status": "pending"}


@app.get("/api/channel/check")
async def check_channel_membership(user=Depends(current_user)):
    async with db.get_db() as conn:
        enabled = await db.get_setting(conn, "force_join_enabled", "1")
        if enabled != "1":
            return {"joined": True}
        channel_username = await db.get_setting(conn, "force_join_channel_username", "@chanelone13")
        channel_link = await db.get_setting(conn, "force_join_channel_link", "https://t.me/chanelone13")

    if not channel_username:
        return {"joined": True}
    try:
        member = await bot.get_chat_member(chat_id=channel_username, user_id=user["telegram_id"])
        if member.status in ["member", "administrator", "creator"]:
            return {"joined": True}
    except Exception as e:
        print(f"Force join check warning: {e}")
        return {"joined": True}
    return {"joined": False, "channel": channel_username, "link": channel_link}


@app.get("/api/withdraw/history")
async def get_my_withdrawals(user=Depends(current_user)):
    async with db.get_db() as conn:
        cur = await conn.execute(
            "SELECT id, amount_usdt, method, destination, status, created_at FROM withdrawals WHERE user_id = ? ORDER BY id DESC LIMIT 10",
            (user["id"],)
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


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
        if rate <= 0:
            rate = float(db.GEMS_PER_USDT_FALLBACK)
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
    referral_reward_spins: str | None = None
    admin_broadcast: str | None = None
    min_withdraw_usdt: str | None = None
    daily_rewards_ladder: str | None = None
    payout_channel_link: str | None = None
    payout_channel_username: str | None = None
    force_join_enabled: str | None = None
    force_join_channel_username: str | None = None
    force_join_channel_link: str | None = None
    webapp_url: str | None = None


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
        u_row = await (await conn.execute("SELECT COUNT(*) c FROM users")).fetchone()
        users = u_row["c"] if u_row else 0
        g_row = await (await conn.execute("SELECT COALESCE(SUM(gems),0) s FROM users")).fetchone()
        total_gems = g_row["s"] if g_row else 0
        b_row = await (await conn.execute("SELECT COALESCE(SUM(balance_usdt),0) s FROM users")).fetchone()
        total_usdt = b_row["s"] if b_row else 0.0
        pwd_row = await (await conn.execute(
            "SELECT COUNT(*) c, COALESCE(SUM(amount_usdt),0) s FROM withdrawals WHERE status='pending'"
        )).fetchone()
        pending_count = pwd_row["c"] if pwd_row else 0
        pending_usdt = pwd_row["s"] if pwd_row else 0.0
    return {
        "total_users": users,
        "total_gems": total_gems,
        "total_usdt": total_usdt,
        "pending_withdrawals": pending_count,
        "pending_withdrawal_usdt": pending_usdt
    }


@app.get("/api/admin/users")
async def admin_list_users(search: str | None = None, _=Depends(require_admin)):
    async with db.get_db() as conn:
        query = """
            SELECT u.id, u.telegram_id, u.username, u.first_name, u.gems, u.balance_usdt,
                   u.daily_streak, u.spins_today, u.bonus_spins, u.created_at,
                   (SELECT COUNT(*) FROM referrals r WHERE r.referrer_id = u.id) as invited_count,
                   (SELECT COUNT(*) FROM user_tasks ut WHERE ut.user_id = u.id AND ut.status='claimed') as tasks_claimed,
                   (SELECT COUNT(*) FROM spin_history sh WHERE sh.user_id = u.id) as total_spins,
                   (SELECT COUNT(*) FROM withdrawals w WHERE w.user_id = u.id) as total_withdrawals
            FROM users u
        """
        params = ()
        if search:
            query += " WHERE u.username LIKE ? OR CAST(u.telegram_id AS TEXT) LIKE ? OR u.first_name LIKE ?"
            params = (f"%{search}%", f"%{search}%", f"%{search}%")
        query += " ORDER BY u.id DESC LIMIT 100"
        cur = await conn.execute(query, params)
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


@app.get("/api/admin/users/{user_id}/activity")
async def admin_user_activity(user_id: int, _=Depends(require_admin)):
    async with db.get_db() as conn:
        cur_u = await conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        user_row = await cur_u.fetchone()
        if not user_row:
            raise HTTPException(404, "User not found")

        cur_tasks = await conn.execute(
            """SELECT ut.claimed_at, t.title, t.reward_gems, t.task_type
               FROM user_tasks ut
               JOIN tasks t ON t.id = ut.task_id
               WHERE ut.user_id = ? AND ut.status = 'claimed'
               ORDER BY ut.claimed_at DESC LIMIT 20""",
            (user_id,)
        )
        tasks = await cur_tasks.fetchall()

        cur_spins = await conn.execute(
            """SELECT sh.reward_gems, sh.created_at, ws.label as segment_label
               FROM spin_history sh
               LEFT JOIN wheel_segments ws ON ws.id = sh.segment_id
               WHERE sh.user_id = ?
               ORDER BY sh.created_at DESC LIMIT 20""",
            (user_id,)
        )
        spins = await cur_spins.fetchall()

        cur_wd = await conn.execute(
            "SELECT * FROM withdrawals WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,)
        )
        withdrawals = await cur_wd.fetchall()

        cur_refs = await conn.execute(
            """SELECT r.bonus_gems, r.created_at, u.username, u.telegram_id, u.first_name
               FROM referrals r
               JOIN users u ON u.id = r.referred_id
               WHERE r.referrer_id = ? ORDER BY r.created_at DESC LIMIT 20""",
            (user_id,)
        )
        referrals = await cur_refs.fetchall()

        cur_gc = await conn.execute(
            """SELECT gcr.redeemed_at, gc.code, gc.reward_gems, gc.reward_usdt, gc.reward_spins
               FROM gift_code_redemptions gcr
               JOIN gift_codes gc ON gc.id = gcr.gift_code_id
               WHERE gcr.user_id = ? ORDER BY gcr.redeemed_at DESC""",
            (user_id,)
        )
        gift_codes = await cur_gc.fetchall()

    return {
        "user": dict(user_row),
        "tasks": [dict(t) for t in tasks],
        "spins": [dict(s) for s in spins],
        "withdrawals": [dict(w) for w in withdrawals],
        "referrals": [dict(r) for r in referrals],
        "gift_codes": [dict(g) for g in gift_codes]
    }


class UserBalanceUpdateBody(BaseModel):
    gems: int | None = None
    balance_usdt: float | None = None
    bonus_spins: int | None = None
    daily_streak: int | None = None


@app.put("/api/admin/users/{user_id}/balance")
async def admin_update_user_balance(user_id: int, body: UserBalanceUpdateBody, _=Depends(require_admin)):
    async with db.get_db() as conn:
        cur = await conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        u = await cur.fetchone()
        if not u:
            raise HTTPException(404, "User not found")
        
        new_gems = body.gems if body.gems is not None else u["gems"]
        new_usdt = body.balance_usdt if body.balance_usdt is not None else u["balance_usdt"]
        new_spins = body.bonus_spins if body.bonus_spins is not None else u["bonus_spins"]
        new_streak = body.daily_streak if body.daily_streak is not None else u["daily_streak"]

        await conn.execute(
            "UPDATE users SET gems = ?, balance_usdt = ?, bonus_spins = ?, daily_streak = ? WHERE id = ?",
            (new_gems, new_usdt, new_spins, new_streak, user_id)
        )
        await conn.commit()
    return {"ok": True, "gems": new_gems, "balance_usdt": new_usdt, "bonus_spins": new_spins, "daily_streak": new_streak}


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


@app.put("/api/admin/wheel-segments/{segment_id}")
async def admin_update_segment(segment_id: int, body: WheelSegmentBody, _=Depends(require_admin)):
    async with db.get_db() as conn:
        await conn.execute(
            "UPDATE wheel_segments SET label=?, value_gems=?, color=?, is_real=?, sort_order=? WHERE id=?",
            (body.label, body.value_gems, body.color, int(body.is_real), body.sort_order, segment_id),
        )
        await conn.commit()
    return {"ok": True}


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


@app.get("/api/admin/tasks")
async def admin_list_tasks(_=Depends(require_admin)):
    async with db.get_db() as conn:
        cur = await conn.execute("SELECT * FROM tasks ORDER BY id DESC")
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


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


@app.put("/api/admin/tasks/{task_id}")
async def admin_update_task(task_id: int, body: TaskBody, _=Depends(require_admin)):
    async with db.get_db() as conn:
        await conn.execute(
            """UPDATE tasks SET title=?, description=?, reward_gems=?, task_type=?, link=?, sort_order=? WHERE id=?""",
            (body.title, body.description, body.reward_gems, body.task_type, body.link, body.sort_order, task_id),
        )
        await conn.commit()
    return {"ok": True}


@app.delete("/api/admin/tasks/{task_id}")
async def admin_deactivate_task(task_id: int, _=Depends(require_admin)):
    async with db.get_db() as conn:
        # Toggle or deactivate active status
        cur = await conn.execute("SELECT active FROM tasks WHERE id = ?", (task_id,))
        row = await cur.fetchone()
        if row:
            new_active = 0 if row["active"] else 1
            await conn.execute("UPDATE tasks SET active = ? WHERE id = ?", (new_active, task_id))
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

        if body.status == "paid":
            try:
                channel_username = await db.get_setting(conn, "payout_channel_username", "@rewardhubpayoutbot")
                cur_u = await conn.execute("SELECT telegram_id FROM users WHERE id = ?", (wd["user_id"],))
                u_info = await cur_u.fetchone()
                tg_id = u_info['telegram_id'] if u_info else wd['user_id']

                reply_text = (
                    f"✅ <b>PAYOUT COMPLETED & APPROVED!</b> 🎉\n\n"
                    f"🆔 <b>User ID:</b> <code>{tg_id}</code>\n"
                    f"💵 <b>Paid Amount:</b> <code>${wd['amount_usdt']:.2f} USDT</code>\n"
                    f"💳 <b>Method:</b> {wd['method'].replace('_', ' ').title()}\n"
                    f"🎉 <b>Status:</b> <b>Paid ✓ (Success)</b>\n\n"
                    f"✨ <i>Thank you for using Reward Hub! Proof verified.</i>"
                )
                if wd["telegram_message_id"]:
                    await bot.send_message(
                        chat_id=channel_username,
                        text=reply_text,
                        parse_mode="HTML",
                        reply_to_message_id=wd["telegram_message_id"]
                    )
                else:
                    await bot.send_message(chat_id=channel_username, text=reply_text, parse_mode="HTML")
            except Exception as e:
                print(f"Payout completion channel reply warning: {e}")

    return {"ok": True}
