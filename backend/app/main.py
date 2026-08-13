import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.bot import run_bot, stop_bot
from app.config import settings
from app.database import init_db
from app.routers import admin, ads, games, referral, tasks, users, wallet

logger = logging.getLogger("uvicorn")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    # Run the Telegram bot's polling loop as a background task inside this
    # same process — this is what lets one Railway service (and one bill)
    # cover both the API and the bot, instead of needing two services.
    bot_task = asyncio.create_task(run_bot())

    def _log_bot_errors(task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"Bot polling task crashed: {exc}")

    bot_task.add_done_callback(_log_bot_errors)

    yield

    # Shutdown: stop polling and close the bot's session cleanly
    bot_task.cancel()
    try:
        await bot_task
    except asyncio.CancelledError:
        pass
    await stop_bot()


app = FastAPI(title="USDT Rewards Mini App API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(users.router)
app.include_router(ads.router)
app.include_router(games.router)
app.include_router(tasks.router)
app.include_router(wallet.router)
app.include_router(referral.router)
app.include_router(admin.router)


@app.get("/")
async def health_check():
    return {"status": "ok", "service": "usdt-rewards-api", "bot": "running in-process"}
