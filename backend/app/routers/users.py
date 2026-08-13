import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Response

from app.auth import get_current_user
from app.bot import fetch_avatar_file_path
from app.config import settings as env_settings
from app.database import get_db, get_settings

router = APIRouter(prefix="/api/user", tags=["user"])

PHOTO_CACHE_TTL = timedelta(hours=6)

# In-memory cache of downloaded avatar bytes, keyed by Telegram's file_path.
# file_path only changes when the user's actual photo changes (see
# _refresh_avatar_if_stale), so it's a safe cache key — no TTL needed here,
# just a size cap so it can't grow unbounded on a long-running process.
_avatar_bytes_cache: dict[str, tuple[bytes, str]] = {}
_AVATAR_CACHE_MAX = 500


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _is_avatar_stale(user: dict) -> bool:
    synced_at = user.get("photo_synced_at")
    if not synced_at:
        return True
    try:
        return datetime.now(timezone.utc) - datetime.fromisoformat(synced_at).replace(tzinfo=timezone.utc) > PHOTO_CACHE_TTL
    except ValueError:
        return True


async def _write_avatar_refresh(telegram_id: int, file_path: str | None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.execute(
            "UPDATE users SET photo_file_path = ?, photo_synced_at = ? WHERE telegram_id = ?",
            (file_path, now, telegram_id),
        )
        await db.commit()


async def _background_refresh_avatar(telegram_id: int) -> None:
    """Fire-and-forget refresh used once a user already has *some* cached
    avatar to show. Runs after the response has gone out, so a slow Telegram
    Bot API round trip never delays app open."""
    try:
        file_path = await fetch_avatar_file_path(telegram_id)
        await _write_avatar_refresh(telegram_id, file_path)
    except Exception as e:
        logging.warning(f"Background avatar refresh failed for {telegram_id}: {e}")


async def _refresh_avatar_if_stale(user: dict) -> dict:
    """Keeps the cached Telegram photo file_path in sync, at most once per
    PHOTO_CACHE_TTL. Only the very first sync for a user (no cached photo to
    fall back to yet) blocks on the Telegram Bot API call; every later
    refresh happens in the background so /sync — and therefore app open —
    is never held up waiting on it."""
    if not _is_avatar_stale(user):
        return user

    if user.get("photo_synced_at") is None:
        # Nothing cached yet, so there's no fast fallback to show — worth
        # the wait just this once.
        file_path = await fetch_avatar_file_path(user["telegram_id"])
        now = datetime.now(timezone.utc).isoformat()
        await _write_avatar_refresh(user["telegram_id"], file_path)
        return {**user, "photo_file_path": file_path, "photo_synced_at": now}

    asyncio.create_task(_background_refresh_avatar(user["telegram_id"]))
    return user


@router.get("/sync")
async def sync_user(user: dict = Depends(get_current_user)):
    """
    Called once when the Mini App opens. get_current_user already created/refreshed
    the row, so we just return the current snapshot the frontend needs to render.
    """
    async with get_db() as db:
        cfg = await get_settings(db)

    user = await _refresh_avatar_if_stale(user)

    return {
        "telegram_id": user["telegram_id"],
        "username": user["username"],
        "first_name": user["first_name"],
        "balance": round(user["balance"], 4),
        "total_earned": round(user["total_earned"], 4),
        "streak_count": user["streak_count"],
        "last_checkin_date": user["last_checkin_date"],
        "checked_in_today": user["last_checkin_date"] == _today_utc(),
        "daily_checkin_enabled": cfg["daily_checkin_enabled"],
        "binance_pay_id": user["binance_pay_id"],
        "referral_link": f"https://t.me/{env_settings.BOT_USERNAME}/{env_settings.MINI_APP_SHORT_NAME}?startapp={user['telegram_id']}",
        "support_username": cfg["support_username"],
        # Relative path — the frontend prepends API_BASE. Never a direct Telegram
        # file URL, since that would embed the bot token in a client-visible link.
        "avatar_url": f"/api/user/avatar/{user['telegram_id']}" if user.get("photo_file_path") else None,
    }


@router.get("/avatar/{telegram_id}")
async def get_avatar(telegram_id: int):
    """
    Proxies a user's Telegram profile photo. This never exposes the bot token to
    the client — the token-bearing Telegram file URL is only ever used in this
    server-to-server request.
    """
    async with get_db() as db:
        cursor = await db.execute("SELECT photo_file_path FROM users WHERE telegram_id = ?", (telegram_id,))
        row = await cursor.fetchone()
    if not row or not row["photo_file_path"]:
        raise HTTPException(status_code=404, detail="No profile photo on file")

    file_path = row["photo_file_path"]
    cached = _avatar_bytes_cache.get(file_path)
    if cached:
        image_bytes, content_type = cached
    else:
        file_url = f"https://api.telegram.org/file/bot{env_settings.BOT_TOKEN}/{file_path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(file_url) as resp:
                if resp.status != 200:
                    raise HTTPException(status_code=404, detail="Profile photo unavailable")
                image_bytes = await resp.read()
                content_type = resp.headers.get("Content-Type", "image/jpeg")

        if len(_avatar_bytes_cache) >= _AVATAR_CACHE_MAX:
            _avatar_bytes_cache.clear()
        _avatar_bytes_cache[file_path] = (image_bytes, content_type)

    return Response(content=image_bytes, media_type=content_type, headers={"Cache-Control": "public, max-age=21600"})


@router.post("/checkin")
async def daily_checkin(user: dict = Depends(get_current_user)):
    today = _today_utc()
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    if user["last_checkin_date"] == today:
        raise HTTPException(status_code=400, detail="Already checked in today")

    async with get_db() as db:
        cfg = await get_settings(db)
        if not cfg["daily_checkin_enabled"]:
            raise HTTPException(status_code=403, detail="Daily check-in is currently disabled")

        # Streak continues only if the last check-in was yesterday; otherwise it resets to day 1
        new_streak = user["streak_count"] + 1 if user["last_checkin_date"] == yesterday else 1
        streak_rewards = cfg["streak_rewards"] or [0.002]
        reward = streak_rewards[(new_streak - 1) % len(streak_rewards)]

        new_balance = user["balance"] + reward
        await db.execute(
            """UPDATE users SET balance = ?, total_earned = total_earned + ?,
               streak_count = ?, last_checkin_date = ? WHERE telegram_id = ?""",
            (new_balance, reward, new_streak, today, user["telegram_id"]),
        )
        await db.execute(
            """INSERT INTO transactions (telegram_id, type, amount, balance_after, meta)
               VALUES (?, 'checkin', ?, ?, ?)""",
            (user["telegram_id"], reward, new_balance, json.dumps({"streak_day": new_streak})),
        )
        await db.commit()

    return {"reward": reward, "new_balance": round(new_balance, 4), "streak_count": new_streak}


@router.get("/transactions")
async def get_transactions(user: dict = Depends(get_current_user), limit: int = 50):
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT type, amount, balance_after, created_at FROM transactions
               WHERE telegram_id = ? ORDER BY id DESC LIMIT ?""",
            (user["telegram_id"], min(limit, 200)),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]
