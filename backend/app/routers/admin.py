from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from app.auth import verify_admin
from app.database import get_db

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
