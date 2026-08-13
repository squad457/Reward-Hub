import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from app.auth import get_current_user
from app.database import get_db, get_settings

router = APIRouter(prefix="/api/users", tags=["users"])


def _today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@router.get("/me")
async def get_me(user: dict = Depends(get_current_user)):
    today = _today_str()
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) as c FROM ad_events WHERE telegram_id = ? AND created_at >= ?",
            (user["telegram_id"], (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()),
        )
        ads_watched_today = (await cursor.fetchone())["c"]

    enriched = dict(user)
    enriched["streak_claimed_today"] = user.get("last_checkin_date") == today
    enriched["ads_watched_today"] = ads_watched_today
    return {"user": enriched}


@router.post("/streak/claim")
async def claim_streak(user: dict = Depends(get_current_user)):
    telegram_id = user["telegram_id"]
    today = _today_str()
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    async with get_db() as db:
        cfg = await get_settings(db)
        if not cfg["daily_checkin_enabled"]:
            raise HTTPException(status_code=403, detail="Daily check-in is currently disabled")

        # Re-read the row so we're not trusting a possibly-stale `last_checkin_date`
        # from before this request — this is also what stops the same day being
        # claimed twice (the previous version had no such check at all).
        cursor = await db.execute(
            "SELECT balance, streak_count, last_checkin_date FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cursor.fetchone()
        if row["last_checkin_date"] == today:
            raise HTTPException(status_code=409, detail="Already claimed today, come back tomorrow")

        # Streak continues only if the last claim was yesterday; any bigger gap
        # (or first-ever claim) restarts it at day 1.
        new_streak = row["streak_count"] + 1 if row["last_checkin_date"] == yesterday else 1

        rewards = cfg["streak_rewards"] or [0.002]
        reward = rewards[min(new_streak - 1, len(rewards) - 1)]

        new_balance = row["balance"] + reward
        await db.execute(
            """UPDATE users SET balance = ?, total_earned = total_earned + ?,
                                 streak_count = ?, last_checkin_date = ?
               WHERE telegram_id = ?""",
            (new_balance, reward, new_streak, today, telegram_id),
        )
        await db.execute(
            """INSERT INTO transactions (telegram_id, type, amount, balance_after, meta)
               VALUES (?, 'checkin', ?, ?, ?)""",
            (telegram_id, reward, new_balance, json.dumps({"streak_day": new_streak})),
        )
        await db.commit()

    return {"reward": reward, "streak_count": new_streak, "new_balance": round(new_balance, 4)}
