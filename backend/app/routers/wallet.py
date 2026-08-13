from fastapi import APIRouter, Depends, HTTPException

from app.auth import get_current_user
from app.database import get_db, get_settings
from app.models import WithdrawalRequest

router = APIRouter(prefix="/api/wallet", tags=["wallet"])


@router.post("/withdraw")
async def request_withdrawal(payload: WithdrawalRequest, user: dict = Depends(get_current_user)):
    async with get_db() as db:
        cfg = await get_settings(db)

        if payload.amount < cfg["min_withdrawal_usdt"]:
            raise HTTPException(status_code=400, detail=f"Minimum withdrawal is ${cfg['min_withdrawal_usdt']}")
        if payload.amount not in cfg["withdrawal_tiers"]:
            raise HTTPException(status_code=400, detail=f"Amount must be one of {cfg['withdrawal_tiers']}")
        if payload.amount > user["balance"]:
            raise HTTPException(status_code=400, detail="Insufficient balance")
        if payload.method == "usdt_address" and not payload.network:
            raise HTTPException(status_code=400, detail="Network is required for wallet address withdrawals")

        # Reserve the funds immediately so the same balance can't be withdrawn twice
        # while this request is pending admin review.
        new_balance = user["balance"] - payload.amount
        await db.execute(
            "UPDATE users SET balance = ?, binance_pay_id = ? WHERE telegram_id = ?",
            (new_balance, payload.payout_id if payload.method == "binance_pay" else user["binance_pay_id"],
             user["telegram_id"]),
        )
        await db.execute(
            """INSERT INTO withdrawals (telegram_id, amount, method, payout_id, network, status)
               VALUES (?, ?, ?, ?, ?, 'pending')""",
            (user["telegram_id"], payload.amount, payload.method, payload.payout_id, payload.network),
        )
        await db.execute(
            """INSERT INTO transactions (telegram_id, type, amount, balance_after, meta)
               VALUES (?, 'withdrawal', ?, ?, '{}')""",
            (user["telegram_id"], -payload.amount, new_balance),
        )
        await db.commit()

    return {"status": "pending", "new_balance": round(new_balance, 4)}


@router.get("/withdrawals")
async def withdrawal_history(user: dict = Depends(get_current_user)):
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT id, amount, method, payout_id, network, status, requested_at, resolved_at
               FROM withdrawals WHERE telegram_id = ? ORDER BY id DESC""",
            (user["telegram_id"],),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


@router.get("/config")
async def wallet_config():
    """Frontend uses this to render the amount tier buttons without hardcoding them."""
    async with get_db() as db:
        cfg = await get_settings(db)
    return {
        "min_withdrawal": cfg["min_withdrawal_usdt"],
        "tiers": cfg["withdrawal_tiers"],
        "support_username": cfg["support_username"],
    }
