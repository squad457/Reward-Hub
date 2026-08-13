"""Pydantic schemas for request bodies and typed responses."""
from pydantic import BaseModel, Field, field_validator


class AdRewardPayload(BaseModel):
    """Sent by the frontend right after Adsgram's SDK fires its onReward callback."""
    reward_event: str = Field(..., description="Unique id Adsgram gives for this ad view, prevents double-crediting")


class TaskCompletePayload(BaseModel):
    task_id: int


class GamePlayPayload(BaseModel):
    """Sent when playing Spin or Scratch. ad_reward_event is required only once the
    user's free daily plays are used up and the game requires watching an ad to continue.
    cells is used by Scratch only: the exact 3 cell indices (0-8) the player tapped."""
    ad_reward_event: str | None = None
    cells: list[int] | None = None


class WithdrawalRequest(BaseModel):
    amount: float
    method: str = Field(default="binance_pay", pattern="^(binance_pay|usdt_address)$")
    payout_id: str = Field(..., min_length=3, max_length=128)
    network: str | None = Field(default=None, description="Required when method = usdt_address, e.g. TRC20")

    @field_validator("payout_id")
    @classmethod
    def strip_payout_id(cls, v: str) -> str:
        return v.strip()


class WithdrawalStatusUpdate(BaseModel):
    status: str = Field(..., pattern="^(approved|rejected)$")
    admin_note: str | None = None


class TaskCreate(BaseModel):
    title: str
    description: str | None = None
    url: str
    reward: float
    task_type: str = Field(default="link", pattern="^(link|telegram_join)$")


class SettingsUpdate(BaseModel):
    """All fields optional — the admin dashboard only sends the keys it changed."""
    ads_enabled: bool | None = None
    adsgram_block_id: str | None = None
    ad_reward_usdt: float | None = None
    ad_daily_limit: int | None = None
    ad_cooldown_seconds: int | None = None
    referral_commission_percent: float | None = None
    referral_signup_bonus: float | None = None
    referral_fixed_reward: float | None = None
    min_withdrawal_usdt: float | None = None
    withdrawal_tiers: list[float] | None = None
    streak_rewards: list[float] | None = None
    daily_checkin_enabled: bool | None = None
    support_username: str | None = None
    maintenance_mode: bool | None = None
    maintenance_message: str | None = None
    adsgram_debug: bool | None = None

    spin_enabled: bool | None = None
    spin_min_reward: float | None = None
    spin_max_reward: float | None = None
    spin_segments: list[float] | None = None
    spin_daily_free_spins: int | None = None
    spin_max_daily_spins: int | None = None
    spin_require_ad_after_free: bool | None = None
    spin_cooldown_seconds: int | None = None

    scratch_enabled: bool | None = None
    scratch_min_reward: float | None = None
    scratch_max_reward: float | None = None
    scratch_daily_free: int | None = None
    scratch_max_daily: int | None = None
    scratch_require_ad_after_free: bool | None = None
    # How many of the 9 cells are hidden diamonds each round. Players always
    # get exactly 3 taps regardless of this number; it only tunes difficulty
    # (more hidden diamonds = easier to hit some of them in 3 taps). 1-9.
    scratch_winning_cells: int | None = None

    @field_validator("scratch_winning_cells")
    @classmethod
    def check_scratch_winning_cells(cls, v):
        if v is not None and not (1 <= v <= 9):
            raise ValueError("scratch_winning_cells must be between 1 and 9")
        return v

    # Reward ranges (spin/scratch min & max) are intentionally NOT validated here.
    # If an admin submits them reversed, /api/admin/settings auto-swaps them
    # instead of rejecting the request — see update_settings in routers/admin.py.


class UserAdjustBalance(BaseModel):
    telegram_id: int
    amount: float = Field(..., description="Positive to credit, negative to debit")
    note: str | None = None


class UserBanToggle(BaseModel):
    telegram_id: int
    is_banned: bool


class BroadcastPayload(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
