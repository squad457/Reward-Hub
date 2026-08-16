"""
bot.py
aiogram bot for Reward Hub. Handles /start (with optional referral deep-link)
and shows the button that opens the Mini App.

Env vars required:
    BOT_TOKEN     - Telegram bot token
    WEBAPP_URL    - deployed frontend URL, e.g. https://reward-hub-production.up.railway.app
"""

import asyncio
import os

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

import database as db

BOT_TOKEN = os.environ.get("BOT_TOKEN", "123456:TEST_TOKEN")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://usdtreward.online")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


async def get_webapp_url() -> str:
    try:
        async with db.get_db() as conn:
            url = await db.get_setting(conn, "webapp_url", "")
            if url:
                return url
    except Exception:
        pass
    return WEBAPP_URL


@dp.message(CommandStart())
async def start(message: Message, command: CommandObject):
    ref_code = command.args
    base_url = await get_webapp_url()
    url = base_url if not ref_code else f"{base_url}?ref={ref_code}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Open Reward Hub", web_app=WebAppInfo(url=url))]
    ])

    await message.answer(
        "Welcome to <b>Reward Hub</b> 🎁\n\n"
        "Engage with community tasks, spin the daily wheel, collect reward gems, and connect with friends!",
        reply_markup=kb,
        parse_mode="HTML",
    )


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
