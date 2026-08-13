import json
from datetime import datetime, timedelta, timezone

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Response
from app.auth import get_current_user
from app.bot import fetch_avatar_file_path, bot
from app.config import settings
from app.database import get_db, get_settings

router = APIRouter(prefix="/api/users", tags=["users"])

# How long a cached Telegram file_path is trusted before we re-resolve it via
# getUserProfilePhotos. Telegram file_paths themselves can go stale after a
# while, so this isn't "forever" even for users who never change their photo.
AVATAR_CACHE_TTL = timedelta(hours=6)


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


@router.get("/activity")
async def get_activity(limit: int = 12, user: dict = Depends(get_current_user)):
    """Real transaction history — replaces the old static 'Mission Log' placeholder
    on the home screen, which never reflected anything the user actually did."""
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT type, amount, created_at FROM transactions WHERE telegram_id = ? ORDER BY id DESC LIMIT ?",
            (user["telegram_id"], limit),
        )
        rows = await cursor.fetchall()
        return [{"type": r["type"], "amount": r["amount"], "created_at": r["created_at"]} for r in rows]


@router.get("/avatar/{telegram_id}")
async def get_avatar(telegram_id: int):
    """
    Proxies the user's Telegram profile photo. The bot token must never reach
    the browser, so this fetches the image bytes server-side and streams them
    back — the frontend only ever sees this URL, never a t.me/file link.
    Cached in the users table (photo_file_path/photo_synced_at) so we don't
    call getUserProfilePhotos on every single avatar load.
    """
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT photo_file_path, photo_synced_at FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cursor.fetchone()
        file_path = row["photo_file_path"] if row else None
        synced_at = row["photo_synced_at"] if row else None

        stale = True
        if synced_at:
            try:
                stale = datetime.fromisoformat(synced_at) < datetime.now(timezone.utc) - AVATAR_CACHE_TTL
            except ValueError:
                stale = True

        if stale or not file_path:
            file_path = await fetch_avatar_file_path(telegram_id)
            if row is not None:
                await db.execute(
                    "UPDATE users SET photo_file_path = ?, photo_synced_at = ? WHERE telegram_id = ?",
                    (file_path, datetime.now(timezone.utc).isoformat(), telegram_id),
                )
                await db.commit()

    if not bot or not file_path:
        raise HTTPException(status_code=404, detail="No profile photo available")

    file_url = f"https://api.telegram.org/file/bot{settings.BOT_TOKEN}/{file_path}"
    async with aiohttp.ClientSession() as session:
        async with session.get(file_url) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=404, detail="Could not fetch profile photo")
            content = await resp.read()
            content_type = resp.headers.get("Content-Type", "image/jpeg")

    return Response(content=content, media_type=content_type, headers={"Cache-Control": "public, max-age=21600"})
