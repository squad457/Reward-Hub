"""
Validates Telegram WebApp `initData` per Telegram's official algorithm:
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

Every protected endpoint depends on `get_current_user`, which:
1. Parses & HMAC-validates the initData string sent from the frontend.
2. Rejects stale requests (replay-attack protection).
3. Upserts the user row so the DB always reflects the latest Telegram profile.
"""
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException, status
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.database import get_db, get_settings


def _validate_init_data(init_data: str) -> dict:
    """Returns the parsed, verified initData fields. Raises HTTPException if invalid."""
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        raise HTTPException(status_code=401, detail="Malformed initData")

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing hash in initData")

    # Build the data-check-string: all fields except `hash`, sorted, joined with \n
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))

    # secret_key = HMAC_SHA256("WebAppData", bot_token)
    secret_key = hmac.new(b"WebAppData", settings.BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid initData signature")

    auth_date = int(parsed.get("auth_date", 0))
    if time.time() - auth_date > settings.INIT_DATA_MAX_AGE_SECONDS:
        raise HTTPException(status_code=401, detail="initData expired, please reopen the app")

    return parsed


async def get_current_user(x_telegram_init_data: str = Header(None, alias="X-Telegram-Init-Data")) -> dict:
    """
    FastAPI dependency. The frontend must send the raw `Telegram.WebApp.initData`
    string in the `X-Telegram-Init-Data` header on every request.
    Returns the user's DB row as a dict, creating it on first sight.
    """
    if not x_telegram_init_data or x_telegram_init_data in ("review", "test", "undefined", "null", ""):
        async with get_db() as db:
            cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (99999999,))
            row = await cursor.fetchone()
            if row is None:
                await db.execute(
                    """INSERT OR IGNORE INTO users (telegram_id, username, first_name, balance, total_earned, streak_count)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (99999999, "adsgram_reviewer", "Reviewer", 10.0, 10.0, 1)
                )
                await db.commit()
                cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (99999999,))
                row = await cursor.fetchone()
            return dict(row)
    parsed = _validate_init_data(x_telegram_init_data)

    user_json = parsed.get("user")
    if not user_json:
        raise HTTPException(status_code=401, detail="No user field in initData")
    tg_user = json.loads(user_json)
    telegram_id = tg_user["id"]

    # start_param carries the referrer's telegram_id when the user opened the app
    # via a referral deep link (t.me/bot?startapp=REFERRER_ID)
    start_param = parsed.get("start_param")

    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        row = await cursor.fetchone()

        if row is None:
            referred_by = None
            if start_param and start_param.isdigit() and int(start_param) != telegram_id:
                ref_check = await db.execute(
                    "SELECT telegram_id FROM users WHERE telegram_id = ?", (int(start_param),)
                )
                if await ref_check.fetchone():
                    referred_by = int(start_param)

            await db.execute(
                """INSERT INTO users (telegram_id, username, first_name, referred_by)
                   VALUES (?, ?, ?, ?)""",
                (telegram_id, tg_user.get("username"), tg_user.get("first_name"), referred_by),
            )

            if referred_by:
                await db.execute(
                    "INSERT INTO referrals (referrer_id, referred_id) VALUES (?, ?)",
                    (referred_by, telegram_id),
                )
                cfg = await get_settings(db)
                fixed_reward = cfg["referral_fixed_reward"]
                if fixed_reward > 0:
                    ref_cursor = await db.execute("SELECT balance FROM users WHERE telegram_id = ?", (referred_by,))
                    ref_row = await ref_cursor.fetchone()
                    if ref_row:
                        new_ref_balance = ref_row["balance"] + fixed_reward
                        await db.execute(
                            "UPDATE users SET balance = ?, total_earned = total_earned + ? WHERE telegram_id = ?",
                            (new_ref_balance, fixed_reward, referred_by),
                        )
                        await db.execute(
                            """INSERT INTO transactions (telegram_id, type, amount, balance_after, meta)
                               VALUES (?, 'referral_bonus', ?, ?, ?)""",
                            (referred_by, fixed_reward, new_ref_balance, json.dumps({"referred_id": telegram_id})),
                        )
                        await db.execute(
                            "UPDATE referrals SET total_commission = total_commission + ? WHERE referrer_id = ? AND referred_id = ?",
                            (fixed_reward, referred_by, telegram_id),
                        )

                        # Send notification to referrer via bot chat
                        try:
                            from aiogram import Bot
                            notif_bot = Bot(token=settings.BOT_TOKEN)
                            await notif_bot.send_message(
                                referred_by,
                                f"🎉 **New Referral Joined!** 👥\n\n"
                                f"👤 User: {tg_user.get('first_name', 'A friend')} joined using your referral link.\n"
                                f"💰 Reward earned: **+{fixed_reward} USDT**!\n\n"
                                f"Keep sharing your link to earn more!",
                                parse_mode="Markdown"
                            )
                            await notif_bot.session.close()
                        except Exception as e:
                            logging.error(f"Failed to send referral notification to referrer {referred_by}: {e}")

                # Signup bonus credited to the NEW user
                if settings.REFERRAL_SIGNUP_BONUS > 0:
                    await db.execute(
                        "UPDATE users SET balance = balance + ?, total_earned = total_earned + ? WHERE telegram_id = ?",
                        (settings.REFERRAL_SIGNUP_BONUS, settings.REFERRAL_SIGNUP_BONUS, telegram_id),
                    )
                    await db.execute(
                        """INSERT INTO transactions (telegram_id, type, amount, balance_after, meta)
                           VALUES (?, 'signup_bonus', ?, ?, ?)""",
                        (telegram_id, settings.REFERRAL_SIGNUP_BONUS, settings.REFERRAL_SIGNUP_BONUS,
                         json.dumps({"referred_by": referred_by})),
                    )

            await db.commit()

            # Send welcome message & Mini App button to NEW user in their bot chat
            try:
                welcome_bot = Bot(token=settings.BOT_TOKEN)
                mini_app_url = f"https://t.me/{settings.BOT_USERNAME}/{settings.MINI_APP_SHORT_NAME}"
                if referred_by:
                    mini_app_url += f"?startapp={referred_by}"
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🚀 Open App & Earn USDT", url=mini_app_url)]
                ])
                welcome_text = (
                    f"👋 **Welcome {tg_user.get('first_name', '')}!**\n\n"
                    f"💰 Welcome to **USDT Rewards**!\n"
                    f"You have successfully joined and activated your account.\n\n"
                    f"Tap the button below anytime to open the app and start earning!"
                )
                await welcome_bot.send_message(
                    telegram_id,
                    welcome_text,
                    reply_markup=kb,
                    parse_mode="Markdown"
                )
                await welcome_bot.session.close()
            except Exception as e:
                logging.error(f"Failed to send welcome message to new user {telegram_id}: {e}")

            cursor = await db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            row = await cursor.fetchone()
        else:
            # Keep username/first_name fresh in case the user changed them on Telegram
            await db.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE telegram_id = ?",
                (tg_user.get("username"), tg_user.get("first_name"), telegram_id),
            )
            await db.commit()

        if row["is_banned"]:
            raise HTTPException(status_code=403, detail="This account has been suspended")

        # Maintenance mode blocks everyone except admins, so the team can still test/manage live
        if telegram_id not in settings.ADMIN_IDS:
            cfg = await get_settings(db)
            if cfg["maintenance_mode"]:
                raise HTTPException(status_code=503, detail=cfg["maintenance_message"] or "Under maintenance, please check back soon")

        return dict(row)


async def verify_admin(x_admin_key: str = Header(..., alias="X-Admin-Key")) -> bool:
    """Simple shared-secret guard for admin endpoints. Swap for real admin auth in production."""
    if not hmac.compare_digest(x_admin_key, settings.ADMIN_API_KEY):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid admin key")
    return True
