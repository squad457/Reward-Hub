import json

from fastapi import APIRouter, Depends, HTTPException
from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import WithdrawPayload

router = APIRouter(prefix="/api/wallet", tags=["wallet"])


@router.post("/withdraw")
async def withdraw(payload: WithdrawPayload, user: dict = Depends(get_current_user)):
    telegram_id = user["telegram_id"]
    address = payload.address.strip()
    if not address:
        raise HTTPException(status_code=400, detail="Wallet address / Pay ID is required")

    min_withdrawal = float(settings.MIN_WITHDRAWAL_USDT or 10.0)

    async with get_db() as db:
        # Re-read the balance inside the transaction instead of trusting the
        # `user` dict captured before this request, so two rapid taps on
        # Withdraw can't both pass the balance check against a stale value.
        cursor = await db.execute(
            "SELECT balance FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cursor.fetchone()
        balance = row["balance"]

        if balance < min_withdrawal:
            raise HTTPException(
                status_code=400,
                detail=f"Balance insufficient. Minimum withdrawal is ${min_withdrawal}",
            )

        amount = round(balance, 4)
        method = "binance_pay" if "binance" in payload.method.lower() else "usdt_address"

        # Deduct immediately and log the withdrawal request — otherwise nothing
        # stops the same balance from being withdrawn (or spent on games/tasks)
        # again while the request is pending admin review.
        new_balance = round(balance - amount, 4)
        await db.execute(
            "UPDATE users SET balance = ? WHERE telegram_id = ?", (new_balance, telegram_id)
        )
        await db.execute(
            """INSERT INTO withdrawals (telegram_id, amount, method, payout_id, status)
               VALUES (?, ?, ?, ?, 'pending')""",
            (telegram_id, amount, method, address),
        )
        await db.execute(
            """INSERT INTO transactions (telegram_id, type, amount, balance_after, meta)
               VALUES (?, 'withdrawal', ?, ?, ?)""",
            (telegram_id, -amount, new_balance, json.dumps({"method": method, "address": address})),
        )
        await db.execute(
            "UPDATE users SET binance_pay_id = ? WHERE telegram_id = ?", (address, telegram_id)
        )
        await db.commit()

    return {"amount": amount, "status": "pending"}
