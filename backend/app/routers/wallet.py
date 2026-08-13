import json

from fastapi import APIRouter, Depends, HTTPException
from app.auth import get_current_user
from app.database import get_db, get_settings
from app.models import WithdrawPayload

router = APIRouter(prefix="/api/wallet", tags=["wallet"])


@router.get("/status")
async def wallet_status(user: dict = Depends(get_current_user)):
    """Frontend reads this to show the real, current admin-configured minimum
    instead of a hardcoded number."""
    async with get_db() as db:
        cfg = await get_settings(db)
    return {"min_withdrawal_usdt": cfg["min_withdrawal_usdt"]}


@router.post("/withdraw")
async def withdraw(payload: WithdrawPayload, user: dict = Depends(get_current_user)):
    telegram_id = user["telegram_id"]
    address = payload.address.strip()
    if not address:
        raise HTTPException(status_code=400, detail="Wallet address / Pay ID is required")

    async with get_db() as db:
        # Was reading settings.MIN_WITHDRAWAL_USDT — the static value baked in at
        # boot from the .env file — so changing "Minimum withdrawal" in the admin
        # dashboard's Settings page (which writes to the settings TABLE) never
        # actually changed anything here. Must read it from get_settings(db) like
        # every other admin-tunable number in the app.
        cfg = await get_settings(db)
        min_withdrawal = cfg["min_withdrawal_usdt"]

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
