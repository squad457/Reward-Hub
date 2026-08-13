from fastapi import APIRouter, Depends, HTTPException
from app.auth import get_current_user
from app.database import get_db

router = APIRouter(prefix="/api/users", tags=["users"])

@router.get("/me")
async def get_me(user: dict = Depends(get_current_user)):
    return {"user": user}

@router.post("/streak/claim")
async def claim_streak(user: dict = Depends(get_current_user)):
    async with get_db() as db:
        # Simple streak claim logic
        reward = 0.002
        new_balance = user["balance"] + reward
        await db.execute(
            "UPDATE users SET balance = ?, total_earned = total_earned + ?, streak_count = streak_count + 1, last_checkin_date = date('now') WHERE telegram_id = ?",
            (new_balance, reward, user["telegram_id"])
        )
        await db.commit()
    return {"reward": reward}