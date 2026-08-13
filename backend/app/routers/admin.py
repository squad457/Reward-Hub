from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from app.auth import verify_admin
from app.database import get_db, get_settings
from app.models import TaskCreatePayload, SettingsUpdatePayload

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/stats")
async def admin_stats(_: bool = Depends(verify_admin)):
    async with get_db() as db:
        cursor = await db.execute("SELECT COUNT(*) as total FROM users")
        total_users = (await cursor.fetchone())["total"]
        cursor = await db.execute(
            "SELECT COUNT(*) as c FROM withdrawals WHERE status = 'pending'"
        )
        pending_withdrawals = (await cursor.fetchone())["c"]
        cursor = await db.execute("SELECT COALESCE(SUM(total_earned),0) as t FROM users")
        total_paid_out = (await cursor.fetchone())["t"]
        return {
            "total_users": total_users,
            "pending_withdrawals": pending_withdrawals,
            "total_earned_all_users": total_paid_out,
        }


@router.get("/withdrawals")
async def list_withdrawals(status: str = "pending", _: bool = Depends(verify_admin)):
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT w.*, u.username, u.first_name FROM withdrawals w
               JOIN users u ON u.telegram_id = w.telegram_id
               WHERE w.status = ? ORDER BY w.requested_at DESC""",
            (status,),
        )
        return [dict(r) for r in await cursor.fetchall()]


@router.post("/withdrawals/{withdrawal_id}/resolve")
async def resolve_withdrawal(
    withdrawal_id: int, approve: bool, note: str = "", _: bool = Depends(verify_admin)
):
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM withdrawals WHERE id = ?", (withdrawal_id,))
        w = await cursor.fetchone()
        if not w:
            raise HTTPException(status_code=404, detail="Withdrawal not found")
        if w["status"] != "pending":
            raise HTTPException(status_code=409, detail="Withdrawal already resolved")

        new_status = "approved" if approve else "rejected"
        await db.execute(
            "UPDATE withdrawals SET status = ?, admin_note = ?, resolved_at = ? WHERE id = ?",
            (new_status, note, datetime.now(timezone.utc).isoformat(), withdrawal_id),
        )
        # Rejections refund the balance that was deducted when the withdrawal was requested.
        if not approve:
            await db.execute(
                "UPDATE users SET balance = balance + ? WHERE telegram_id = ?",
                (w["amount"], w["telegram_id"]),
            )
        await db.commit()
    return {"status": new_status}


@router.get("/users")
async def list_users(q: str = "", limit: int = 50, _: bool = Depends(verify_admin)):
    async with get_db() as db:
        if q:
            cursor = await db.execute(
                """SELECT telegram_id, username, first_name, balance, total_earned, is_banned
                   FROM users WHERE username LIKE ? OR CAST(telegram_id AS TEXT) LIKE ?
                   ORDER BY created_at DESC LIMIT ?""",
                (f"%{q}%", f"%{q}%", limit),
            )
        else:
            cursor = await db.execute(
                """SELECT telegram_id, username, first_name, balance, total_earned, is_banned
                   FROM users ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            )
        return [dict(r) for r in await cursor.fetchall()]


@router.post("/users/{telegram_id}/ban")
async def set_ban(telegram_id: int, banned: bool, _: bool = Depends(verify_admin)):
    async with get_db() as db:
        await db.execute(
            "UPDATE users SET is_banned = ? WHERE telegram_id = ?", (1 if banned else 0, telegram_id)
        )
        await db.commit()
    return {"telegram_id": telegram_id, "is_banned": banned}


# ───────────────────────── Tasks ─────────────────────────

@router.get("/tasks")
async def admin_list_tasks(_: bool = Depends(verify_admin)):
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM tasks ORDER BY id DESC")
        return [dict(r) for r in await cursor.fetchall()]


@router.post("/tasks")
async def admin_create_task(payload: TaskCreatePayload, _: bool = Depends(verify_admin)):
    async with get_db() as db:
        cursor = await db.execute(
            """INSERT INTO tasks (title, description, url, reward, task_type)
               VALUES (?, ?, ?, ?, ?)""",
            (payload.title, payload.description, payload.url, payload.reward, payload.task_type),
        )
        await db.commit()
        return {"id": cursor.lastrowid}


@router.post("/tasks/{task_id}/toggle")
async def admin_toggle_task(task_id: int, active: bool, _: bool = Depends(verify_admin)):
    async with get_db() as db:
        cursor = await db.execute("SELECT id FROM tasks WHERE id = ?", (task_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Task not found")
        await db.execute("UPDATE tasks SET is_active = ? WHERE id = ?", (1 if active else 0, task_id))
        await db.commit()
    return {"id": task_id, "is_active": active}


# ───────────────────────── Runtime settings ─────────────────────────
# These are the DB-backed knobs (ad payout, spin/scratch ranges, maintenance
# mode, etc.) described in database.py's DEFAULT_SETTINGS — previously there
# was no way to read or change them without touching the DB directly.

@router.get("/settings")
async def admin_get_settings(_: bool = Depends(verify_admin)):
    async with get_db() as db:
        return await get_settings(db)


@router.post("/settings")
async def admin_update_settings(payload: SettingsUpdatePayload, _: bool = Depends(verify_admin)):
    if not payload.values:
        raise HTTPException(status_code=400, detail="No settings provided")
    async with get_db() as db:
        for key, value in payload.values.items():
            # Lists (e.g. spin_segments, streak_rewards) arrive as arrays from the
            # dashboard's form fields — store everything as the same comma-joined
            # string format get_settings() already knows how to parse back out.
            stored = ",".join(str(v) for v in value) if isinstance(value, list) else str(value)
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, stored),
            )
        await db.commit()
        return await get_settings(db)
