"""
Central configuration, loaded from environment variables.
On Railway, set these under the service's Variables tab.
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # --- Telegram ---
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")  # from @BotFather
    BOT_USERNAME: str = os.getenv("BOT_USERNAME", "your_bot")  # without @, used to build referral links

    # --- Database ---
    DB_PATH: str = os.getenv("DB_PATH", "app_data.db")

    # --- CORS ---
    # Comma-separated list of allowed frontend origins, e.g. your Vercel domain
    ALLOWED_ORIGINS: list[str] = [
        o.strip() for o in os.getenv(
            "ALLOWED_ORIGINS",
            "https://your-frontend.vercel.app,http://localhost:5173"
        ).split(",") if o.strip()
    ]

    # The short name you gave your Mini App in BotFather's /newapp flow.
    # Used to build both the bot's "Open App" button and every referral link,
    # so they can never drift out of sync with each other.
    MINI_APP_SHORT_NAME: str = os.getenv("MINI_APP_SHORT_NAME", "app")

    # --- Admin ---
    ADMIN_IDS: list[int] = [
        int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
    ]
    ADMIN_API_KEY: str = os.getenv("ADMIN_API_KEY", "change-me-in-production")

    # --- Adsgram ---
    ADSGRAM_BLOCK_ID: str = os.getenv("ADSGRAM_BLOCK_ID", "")
    # Optional: only needed if your Adsgram plan supports signed postbacks.
    # By default Adsgram's Reward URL only substitutes a bare `[userId]` macro with
    # NO signature (see /api/ads/p) — this secret is only used if you've added a
    # custom `signature` param yourself in front of Adsgram (e.g. via a proxy/tracker).
    ADSGRAM_CALLBACK_SECRET: str = os.getenv("ADSGRAM_CALLBACK_SECRET", "change-me")
    # Debug/test mode passed to the Adsgram SDK. IMPORTANT: Adsgram does NOT count debug
    # impressions in your stats and never fires the Reward URL for them — leaving this on
    # is a common cause of an account showing 0 real conversions. Must be off in production.
    ADSGRAM_DEBUG: bool = os.getenv("ADSGRAM_DEBUG", "0") == "1"

    # --- Economy ---
    AD_REWARD_USDT: float = float(os.getenv("AD_REWARD_USDT", "0.003"))
    AD_DAILY_LIMIT: int = int(os.getenv("AD_DAILY_LIMIT", "50"))
    AD_COOLDOWN_SECONDS: int = int(os.getenv("AD_COOLDOWN_SECONDS", "15"))

    REFERRAL_COMMISSION_PERCENT: float = float(os.getenv("REFERRAL_COMMISSION_PERCENT", "10"))
    REFERRAL_SIGNUP_BONUS: float = float(os.getenv("REFERRAL_SIGNUP_BONUS", "0.01"))

    MIN_WITHDRAWAL_USDT: float = float(os.getenv("MIN_WITHDRAWAL_USDT", "10"))
    WITHDRAWAL_TIERS: list[float] = [10, 50, 100]

    # Daily check-in streak rewards, index 0 = day 1 ... index 6 = day 7 (then it loops)
    STREAK_REWARDS: list[float] = [0.002, 0.003, 0.004, 0.005, 0.006, 0.008, 0.02]

    # --- Spin Wheel game ---
    SPIN_ENABLED: bool = os.getenv("SPIN_ENABLED", "1") == "1"
    # The reward paid is EXACTLY the number on the segment the wheel lands on —
    # but the wheel can only ever LAND on a segment whose number falls inside
    # this admin-set [min, max] range. Segments outside the range still show on
    # the wheel (so the admin can add "jackpot"/"try again" style decorative
    # numbers) but are never selected as the actual outcome, so real money can
    # never scatter outside the approved range.
    SPIN_MIN_REWARD: float = float(os.getenv("SPIN_MIN_REWARD", "0.09"))
    SPIN_MAX_REWARD: float = float(os.getenv("SPIN_MAX_REWARD", "0.5"))
    SPIN_SEGMENTS: list[float] = [0.09, 0.5, 0.15, 0.3, 0.1, 0.5, 0.2, 0.4]
    SPIN_DAILY_FREE_SPINS: int = int(os.getenv("SPIN_DAILY_FREE_SPINS", "1"))
    SPIN_MAX_DAILY_SPINS: int = int(os.getenv("SPIN_MAX_DAILY_SPINS", "8"))  # 0 = unlimited (still gated by ads)
    SPIN_REQUIRE_AD_AFTER_FREE: bool = os.getenv("SPIN_REQUIRE_AD_AFTER_FREE", "1") == "1"
    SPIN_COOLDOWN_SECONDS: int = int(os.getenv("SPIN_COOLDOWN_SECONDS", "3"))

    # --- Scratch Card game ---
    SCRATCH_ENABLED: bool = os.getenv("SCRATCH_ENABLED", "1") == "1"
    SCRATCH_MIN_REWARD: float = float(os.getenv("SCRATCH_MIN_REWARD", "0.002"))
    SCRATCH_MAX_REWARD: float = float(os.getenv("SCRATCH_MAX_REWARD", "0.008"))
    SCRATCH_DAILY_FREE: int = int(os.getenv("SCRATCH_DAILY_FREE", "1"))
    SCRATCH_MAX_DAILY: int = int(os.getenv("SCRATCH_MAX_DAILY", "5"))
    SCRATCH_REQUIRE_AD_AFTER_FREE: bool = os.getenv("SCRATCH_REQUIRE_AD_AFTER_FREE", "1") == "1"

    # --- Security ---
    # Reject Telegram initData older than this (seconds) to prevent replay attacks
    INIT_DATA_MAX_AGE_SECONDS: int = int(os.getenv("INIT_DATA_MAX_AGE_SECONDS", "86400"))
    JWT_SECRET: str = os.getenv("JWT_SECRET", "change-me-too")


settings = Settings()
