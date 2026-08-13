from fastapi import APIRouter, Depends

from app.auth import get_current_user
from app.config import settings as env_settings
from app.database import get_db, get_settings

router = APIRouter(prefix="/api/referral", tags=["referral"])


@router.get("/stats")
async def referral_stats(user: dict = Depends(get_current_user)):
    async with get_db() as db:
        cfg = await get_settings(db)

        count_cursor = await db.execute(
            "SELECT COUNT(*) as c FROM referrals WHERE referrer_id = ?", (user["telegram_id"],)
        )
        total_referrals = (await count_cursor.fetchone())["c"]

        earnings_cursor = await db.execute(
            "SELECT COALESCE(SUM(total_commission), 0) as total FROM referrals WHERE referrer_id = ?",
            (user["telegram_id"],),
        )
        total_commission = (await earnings_cursor.fetchone())["total"]

        recent_cursor = await db.execute(
            """SELECT u.first_name, u.username, r.created_at, r.total_commission
               FROM referrals r JOIN users u ON u.telegram_id = r.referred_id
               WHERE r.referrer_id = ? ORDER BY r.id DESC LIMIT 20""",
            (user["telegram_id"],),
        )
        recent = [dict(r) for r in await recent_cursor.fetchall()]

    return {
        "referral_link": f"https://t.me/{env_settings.BOT_USERNAME}/{env_settings.MINI_APP_SHORT_NAME}?startapp={user['telegram_id']}",
        "total_referrals": total_referrals,
        "total_commission_earned": round(total_commission, 4),
        "commission_percent": cfg["referral_commission_percent"],
        "referral_fixed_reward": cfg["referral_fixed_reward"],
        "recent_referrals": recent,
    }
