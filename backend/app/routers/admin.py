"""
Full admin API backing the admin dashboard (frontend/admin.html).
Every route here is guarded by the shared X-Admin-Key header (see auth.verify_admin).
For a bigger team, swap this for real per-admin login + JWT + role checks.
"""
import json

from fastapi import APIRouter, Depends, HTTPException

from app.auth import verify_admin
from app.config import settings as env_settings
from app.database import get_db, get_settings
from app.models import (
    BroadcastPayload,
    SettingsUpdate,
    TaskCreate,
    UserAdjustBalance,
    UserBanToggle,
    WithdrawalStatusUpdate,
)

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(verify_admin)])


# ───────────────────────── Overview ─────────────────────────

@router.get("/stats")
async def platform_stats():
    async with get_db() as db:
        users = (await (await db.execute("SELECT COUNT(*) as c FROM users")).fetchone())["c"]
        banned = (await (await db.execute("SELECT COUNT(*) as c FROM users WHERE is_banned = 1")).fetchone())["c"]
        total_balance = (await (await db.execute(
            "SELECT COALESCE(SUM(balance),0) as t FROM users"
        )).fetchone())["t"]
        total_paid = (await (await db.execute(
            "SELECT COALESCE(SUM(amount),0) as t FROM withdrawals WHERE status='approved'"
        )).fetchone())["t"]
        pending = (await (await db.execute(
            "SELECT COUNT(*) as c FROM withdrawals WHERE status='pending'"
        )).fetchone())["c"]
        pending_amount = (await (await db.execute(
            "SELECT COALESCE(SUM(amount),0) as t FROM withdrawals WHERE status='pending'"
        )).fetchone())["t"]
        total_ads_watched = (await (await db.execute("SELECT COUNT(*) as c FROM ad_events")).fetchone())["c"]
        total_referrals = (await (await db.execute("SELECT COUNT(*) as c FROM referrals")).fetchone())["c"]
        total_spins = (await (await db.execute(
            "SELECT COUNT(*) as c FROM game_events WHERE game_type='spin'"
        )).fetchone())["c"]
        total_spin_payout = (await (await db.execute(
            "SELECT COALESCE(SUM(amount),0) as t FROM game_events WHERE game_type='spin'"
        )).fetchone())["t"]
        total_scratches = (await (await db.execute(
            "SELECT COUNT(*) as c FROM game_events WHERE game_type='scratch'"
        )).fetchone())["c"]
        total_scratch_payout = (await (await db.execute(
            "SELECT COALESCE(SUM(amount),0) as t FROM game_events WHERE game_type='scratch'"
        )).fetchone())["t"]
        # users active in the last 24h based on their most recent transaction
        active_today = (await (await db.execute(
            "SELECT COUNT(DISTINCT telegram_id) as c FROM transactions WHERE created_at >= datetime('now', '-1 day')"
        )).fetchone())["c"]
    return {
        "total_users": users,
        "banned_users": banned,
        "active_today": active_today,
        "total_user_balance": round(total_balance, 2),
        "total_paid_out": round(total_paid, 2),
        "pending_withdrawals": pending,
        "pending_withdrawal_amount": round(pending_amount, 2),
        "total_ads_watched": total_ads_watched,
        "total_referrals": total_referrals,
        "total_spins": total_spins,
        "total_spin_payout": round(total_spin_payout, 4),
        "total_scratches": total_scratches,
        "total_scratch_payout": round(total_scratch_payout, 4),
    }


# ───────────────────────── Settings ─────────────────────────

@router.get("/settings")
async def read_settings():
    async with get_db() as db:
        return await get_settings(db)


def _auto_fit_spin_segments(lo: float, hi: float, total_count: int) -> list[float]:
    """Regenerates the wheel's segment numbers around a reward range.

    Only a handful of segments need to actually be winnable — 3 evenly spaced
    values inside [lo, hi] is plenty of variety for the player. The rest of
    the wheel is filled with bigger, eye-catching "near miss" numbers outside
    the range purely for visual attention (per the module's design: spin_play
    can only ever pay a segment inside [spin_min_reward, spin_max_reward], so
    these decorative slices can never actually be won). Winnable and
    decorative slices are interleaved so the big numbers are spread evenly
    around the wheel instead of clumped together.
    """
    total_count = max(total_count, 6)
    winnable_count = min(3, total_count)
    decorative_count = total_count - winnable_count

    if lo == hi:
        hi = lo + 0.01  # can't spread distinct values across a zero-width range
    if winnable_count == 1:
        winnable = [round((lo + hi) / 2, 4)]
    else:
        step = (hi - lo) / (winnable_count - 1)
        winnable = [round(lo + step * i, 4) for i in range(winnable_count)]

    # Decorative numbers step up well past the max (2x, 3.5x, 5x, ...) so they
    # visually read as tempting "big win" slices without ever being eligible.
    base = hi if hi > 0 else max(lo, 0.01)
    decorative = [round(base * (2 + i * 1.5), 4) for i in range(decorative_count)]

    segments, wi, di = [], 0, 0
    for i in range(winnable_count + decorative_count):
        if i % 2 == 0 and wi < winnable_count:
            segments.append(winnable[wi]); wi += 1
        elif di < decorative_count:
            segments.append(decorative[di]); di += 1
        else:
            segments.append(winnable[wi]); wi += 1
    return segments


@router.post("/settings")
async def update_settings(payload: SettingsUpdate):
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided")

    async with get_db() as db:
        # Auto-detect a reversed reward range (min > max) and swap it instead of
        # rejecting the save. IMPORTANT: only swap when BOTH sides are present in
        # THIS submitted payload — never mix in the old stored value. Comparing
        # a freshly-typed min against a stale, untouched stored max is what used
        # to silently rewrite a number the admin never touched.
        for min_key, max_key in (("spin_min_reward", "spin_max_reward"), ("scratch_min_reward", "scratch_max_reward")):
            if min_key in updates and max_key in updates and updates[min_key] > updates[max_key]:
                updates[min_key], updates[max_key] = updates[max_key], updates[min_key]

        # Guard rail: the wheel can only pay a segment inside [spin_min_reward,
        # spin_max_reward] (see spin_play), and needs at least 2 *distinct*
        # eligible values or every spin would pay the exact same amount —
        # it'd look random (the wheel still spins) but never actually vary.
        # Rather than reject the save and make the admin hand-tune numbers,
        # auto-fit the segments to the new range (see _auto_fit_spin_segments).
        current = await get_settings(db)
        effective_segments = updates.get("spin_segments", current["spin_segments"])
        effective_min = updates.get("spin_min_reward", current["spin_min_reward"])
        effective_max = updates.get("spin_max_reward", current["spin_max_reward"])
        eligible_values = {v for v in effective_segments if effective_min <= v <= effective_max}
        if len(eligible_values) < 2:
            updates["spin_segments"] = _auto_fit_spin_segments(
                effective_min, effective_max, len(effective_segments)
            )

        for key, value in updates.items():
            if isinstance(value, bool):
                stored = "1" if value else "0"
            elif isinstance(value, list):
                stored = ",".join(str(x) for x in value)
            else:
                stored = str(value)
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, stored),
            )
        await db.commit()
        return await get_settings(db)


# ───────────────────────── Withdrawals ─────────────────────────

@router.get("/withdrawals")
async def list_withdrawals(status: str | None = None):
    async with get_db() as db:
        if status:
            cursor = await db.execute(
                """SELECT w.*, u.username, u.first_name FROM withdrawals w
                   JOIN users u ON u.telegram_id = w.telegram_id
                   WHERE w.status = ? ORDER BY w.id DESC""",
                (status,),
            )
        else:
            cursor = await db.execute(
                """SELECT w.*, u.username, u.first_name FROM withdrawals w
                   JOIN users u ON u.telegram_id = w.telegram_id
                   ORDER BY w.id DESC LIMIT 200"""
            )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


@router.patch("/withdrawals/{withdrawal_id}")
async def update_withdrawal(withdrawal_id: int, payload: WithdrawalStatusUpdate):
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM withdrawals WHERE id = ?", (withdrawal_id,))
        withdrawal = await cursor.fetchone()
        if not withdrawal:
            raise HTTPException(status_code=404, detail="Withdrawal not found")
        if withdrawal["status"] != "pending":
            raise HTTPException(status_code=400, detail="Withdrawal already resolved")

        # If rejected, refund the reserved balance back to the user
        if payload.status == "rejected":
            await db.execute(
                "UPDATE users SET balance = balance + ? WHERE telegram_id = ?",
                (withdrawal["amount"], withdrawal["telegram_id"]),
            )

        await db.execute(
            """UPDATE withdrawals SET status = ?, admin_note = ?, resolved_at = datetime('now')
               WHERE id = ?""",
            (payload.status, payload.admin_note, withdrawal_id),
        )
        await db.commit()

    return {"status": payload.status}


# ───────────────────────── Tasks ─────────────────────────

@router.get("/tasks")
async def list_all_tasks():
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM tasks ORDER BY id DESC")
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


@router.post("/tasks")
async def create_task(payload: TaskCreate):
    async with get_db() as db:
        cursor = await db.execute(
            """INSERT INTO tasks (title, description, url, reward, task_type)
               VALUES (?, ?, ?, ?, ?)""",
            (payload.title, payload.description, payload.url, payload.reward, payload.task_type),
        )
        await db.commit()
        return {"id": cursor.lastrowid}


@router.patch("/tasks/{task_id}/toggle")
async def toggle_task(task_id: int):
    async with get_db() as db:
        cursor = await db.execute("SELECT is_active FROM tasks WHERE id = ?", (task_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        new_state = 0 if row["is_active"] else 1
        await db.execute("UPDATE tasks SET is_active = ? WHERE id = ?", (new_state, task_id))
        await db.commit()
    return {"is_active": bool(new_state)}


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: int):
    async with get_db() as db:
        await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await db.execute("DELETE FROM user_tasks WHERE task_id = ?", (task_id,))
        await db.commit()
    return {"deleted": True}


# ───────────────────────── Users ─────────────────────────

@router.get("/users")
async def list_users(search: str | None = None, limit: int = 50):
    async with get_db() as db:
        if search:
            like = f"%{search}%"
            cursor = await db.execute(
                """SELECT telegram_id, username, first_name, balance, total_earned, streak_count,
                          is_banned, created_at FROM users
                   WHERE CAST(telegram_id AS TEXT) LIKE ? OR username LIKE ? OR first_name LIKE ?
                   ORDER BY created_at DESC LIMIT ?""",
                (like, like, like, min(limit, 200)),
            )
        else:
            cursor = await db.execute(
                """SELECT telegram_id, username, first_name, balance, total_earned, streak_count,
                          is_banned, created_at FROM users ORDER BY created_at DESC LIMIT ?""",
                (min(limit, 200),),
            )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


@router.post("/users/ban")
async def toggle_ban(payload: UserBanToggle):
    async with get_db() as db:
        cursor = await db.execute("SELECT telegram_id FROM users WHERE telegram_id = ?", (payload.telegram_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="User not found")
        await db.execute(
            "UPDATE users SET is_banned = ? WHERE telegram_id = ?",
            (1 if payload.is_banned else 0, payload.telegram_id),
        )
        await db.commit()
    return {"is_banned": payload.is_banned}


@router.post("/users/adjust-balance")
async def adjust_balance(payload: UserAdjustBalance):
    async with get_db() as db:
        cursor = await db.execute("SELECT balance FROM users WHERE telegram_id = ?", (payload.telegram_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")

        new_balance = row["balance"] + payload.amount
        if new_balance < 0:
            raise HTTPException(status_code=400, detail="Adjustment would make balance negative")

        # total_earned only moves up, so a debit corrects balance without erasing lifetime stats
        earned_delta = max(payload.amount, 0)
        await db.execute(
            "UPDATE users SET balance = ?, total_earned = total_earned + ? WHERE telegram_id = ?",
            (new_balance, earned_delta, payload.telegram_id),
        )
        await db.execute(
            """INSERT INTO transactions (telegram_id, type, amount, balance_after, meta)
               VALUES (?, 'admin_adjust', ?, ?, ?)""",
            (payload.telegram_id, payload.amount, new_balance, json.dumps({"note": payload.note})),
        )
        await db.commit()
    return {"new_balance": round(new_balance, 4)}


# ───────────────────────── Broadcast ─────────────────────────

@router.post("/broadcast")
async def broadcast_message(payload: BroadcastPayload):
    """Sends a text message to every known user via the bot. Slow for large user bases —
    fine for a few thousand users; swap for a queued job if you outgrow that."""
    try:
        from aiogram import Bot
        from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
    except ImportError:
        raise HTTPException(status_code=500, detail="aiogram not installed")

    async with get_db() as db:
        cursor = await db.execute("SELECT telegram_id FROM users WHERE is_banned = 0")
        user_ids = [r["telegram_id"] for r in await cursor.fetchall()]

    bot = Bot(token=env_settings.BOT_TOKEN)
    sent, failed = 0, 0
    try:
        for uid in user_ids:
            try:
                await bot.send_message(uid, payload.text)
                sent += 1
            except (TelegramForbiddenError, TelegramBadRequest):
                failed += 1
    finally:
        await bot.session.close()

    return {"sent": sent, "failed": failed, "total": len(user_ids)}
