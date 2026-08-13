from fastapi import APIRouter, Depends
from app.auth import get_current_user
from app.config import settings
from app.database import get_db

router = APIRouter(prefix="/api/referral", tags=["referral"])

@router.get("/info")
async def referral_info(user: dict = Depends(get_current_user)):
    async with get_db() as db:
        cursor = await db.execute("SELECT COUNT(*) as count FROM referrals WHERE referrer_id = ?", (user["telegram_id"],))
        count = (await cursor.fetchone())["count"]
        return {
            "referral_link": f"https://t.me/{settings.BOT_USERNAME}?startapp={user['telegram_id']}",
            "total_referred": count,
            "total_earned": user["total_earned"] or 0
        }