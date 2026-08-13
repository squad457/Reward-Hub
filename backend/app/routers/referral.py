from fastapi import APIRouter, Depends
from app.auth import get_current_user
from app.config import settings
from app.database import get_db, get_settings

router = APIRouter(prefix="/api/referral", tags=["referral"])

@router.get("/info")
async def referral_info(user: dict = Depends(get_current_user)):
    async with get_db() as db:
        cursor = await db.execute("SELECT COUNT(*) as count FROM referrals WHERE referrer_id = ?", (user["telegram_id"],))
        count = (await cursor.fetchone())["count"]
        cfg = await get_settings(db)
        return {
            # Must include the mini app's short_name segment — this bot's app was
            # registered via BotFather /newapp (not set as the "main" menu-button
            # app), so a bare "t.me/<bot>?startapp=..." link (no short_name) opens
            # the bot chat but never actually launches the Mini App, and start_param
            # never reaches auth.py — meaning referrals silently never got credited.
            # auth.py's own welcome-message link already builds it this (correct) way.
            "referral_link": f"https://t.me/{settings.BOT_USERNAME}/{settings.MINI_APP_SHORT_NAME}?startapp={user['telegram_id']}",
            "total_referred": count,
            "total_earned": user["total_earned"] or 0,
            # Surfaced so the frontend's invite copy always reflects whatever the
            # admin dashboard has these set to, instead of hardcoded numbers.
            "commission_percent": cfg["referral_commission_percent"],
            "signup_bonus": cfg["referral_fixed_reward"],
        }