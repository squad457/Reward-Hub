"""
Telegram bot logic (aiogram). This module is now imported and run as a
background task INSIDE the FastAPI process (see main.py's lifespan) instead
of needing its own separate Railway service — this keeps you on a single
service, which is cheaper than running two.

If you ever outgrow this (very high traffic, want the bot to restart
independently of the API) you can still run this file standalone with:
    python -m app.bot
That fallback is kept at the bottom of this file.
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings

logging.basicConfig(level=logging.INFO)

bot = None
dp = None
if settings.BOT_TOKEN and ":" in settings.BOT_TOKEN:
    try:
        bot = Bot(token=settings.BOT_TOKEN)
        dp = Dispatcher()
    except Exception as e:
        logging.warning(f"Failed to initialize Telegram Bot: {e}")

MINI_APP_URL = f"https://t.me/{settings.BOT_USERNAME}/{settings.MINI_APP_SHORT_NAME}"


async def fetch_avatar_file_path(telegram_id: int) -> str | None:
    """
    Reliably resolve a user's current Telegram profile photo via the Bot API,
    returning Telegram's internal file_path (NOT a full URL — the full file URL
    contains the bot token, which must never be sent to the frontend/browser;
    see routers/users.py's /avatar/{telegram_id} proxy endpoint for how this is
    served safely).

    We deliberately do NOT rely on Telegram.WebApp.initDataUnsafe.user.photo_url
    on the frontend — per Telegram's own docs that field is only populated when
    the Mini App is launched from the attachment menu, and this bot always
    launches it via a normal "Open App" button, so it was effectively always
    empty. Calling getUserProfilePhotos/getFile with the bot token works
    regardless of how the app was opened.
    """
    if not bot:
        return None
    try:
        photos = await bot.get_user_profile_photos(telegram_id, limit=1)
        if not photos.photos:
            return None
        largest = photos.photos[0][-1]  # last size in the sizes list is the biggest
        file = await bot.get_file(largest.file_id)
        return file.file_path
    except Exception as e:
        logging.warning(f"Could not fetch profile photo for {telegram_id}: {e}")
        return None


def _webapp_keyboard(start_param: str | None = None) -> InlineKeyboardMarkup:
    url = MINI_APP_URL
    if start_param:
        url += f"?startapp={start_param}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎡 Open Reward Hub", url=url)]
    ])


@dp.message(CommandStart()) if dp else lambda f: f
async def start_handler(message: Message):
    # Deep link referrals arrive as /start <referrer_telegram_id>
    referrer_id = None
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip().isdigit():
        referrer_id = parts[1].strip()

    text = (
        f"🎯 Welcome to Reward Hub{', ' + message.from_user.first_name if message.from_user.first_name else ''}!\n\n"
        "🎡 Spin the wheel, scratch for diamonds, and keep your daily earnings streak alive.\n"
        "🎫 Tap below to open your dashboard."
    )
    await message.answer(text, reply_markup=_webapp_keyboard(referrer_id))


async def run_bot():
    """
    Starts polling and runs forever. Call this as an asyncio background task
    from FastAPI's lifespan (see main.py) — do NOT call this and also run
    `python -m app.bot` separately, or Telegram will reject the second
    getUpdates connection (409 Conflict).
    """
    if not bot or not dp:
        logging.warning("Telegram Bot is not initialized (missing or invalid BOT_TOKEN). Bot polling skipped.")
        return
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


async def stop_bot():
    """Called on FastAPI shutdown to close the bot's HTTP session cleanly."""
    if bot:
        await bot.session.close()


# ── Standalone fallback ──────────────────────────────────────────────
# Only used if you deliberately choose to run the bot as its own process
# again later (e.g. `python -m app.bot`). Not used when run.py imports
# run_bot() into the API process, which is the default setup now.
if __name__ == "__main__":
    async def _main():
        try:
            await run_bot()
        finally:
            await stop_bot()

    asyncio.run(_main())
