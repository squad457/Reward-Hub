"""
In-app games: Spin Wheel and Scratch Card.

Design (per product requirement):
- Spin Wheel: the admin sets a payout range (spin_min_reward, spin_max_reward —
  e.g. 0.09-0.5 USDT). The wheel can only ever LAND on a segment whose number
  falls inside that range, and the reward paid is EXACTLY that segment's
  number — so what the user sees is always what they get, and money can never
  scatter outside the admin-approved range. Segments outside the range can
  still be shown on the wheel as decorative "near miss" numbers (e.g. a big
  cosmetic jackpot number, or 0.00), but they can never actually be won.
- Scratch Card: a player always gets exactly 3 taps on the 9-cell board. The
  admin-configured number of diamonds (scratch_winning_cells) is placed at
  random positions on the board *after* the player has chosen their 3 cells,
  so the outcome is genuinely random and can't be predicted or farmed. The
  reward is tiered by how many of the 3 tapped cells turn out to hold a
  diamond (0 = no reward, 1 = small, 2 = medium, 3 = the best tier) and is
  always drawn from a slice that stays fully inside the admin's
  [scratch_min_reward, scratch_max_reward] range. The full board (all diamond
  positions, not just the tapped ones) is always returned so the frontend can
  reveal exactly where the diamonds were, whether the player won or not.
- Each user gets a small number of free plays per day (admin-configurable).
  Once those are used, playing again requires watching a rewarded Adsgram ad
  first — the frontend calls showRewardedAd() and passes the resulting
  reward_event here, which is checked for uniqueness so the same ad view can't
  unlock more than one extra play.
- An optional hard daily cap (spin_max_daily_spins / scratch_max_daily) still
  applies even to ad-unlocked plays, so the games can't be farmed indefinitely.
"""
import json
import random
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

from app.auth import get_current_user
from app.database import get_db, get_settings
from app.models import GamePlayPayload

router = APIRouter(prefix="/api/games", tags=["games"])

# A round is always exactly 3 taps on the 9-cell board — fixed by game design,
# not admin-configurable, so the reward tiers below (0/1/2/3 hits) stay meaningful.
SCRATCH_TAPS_ALLOWED = 3


def _scratch_reward_for_hits(hits: int, min_r: float, max_r: float) -> float:
    """Reward is drawn from a slice of the admin's [min_r, max_r] range sized by
    how many of the player's 3 taps landed on a diamond. Never leaves that range."""
    if max_r < min_r:
        min_r, max_r = max_r, min_r
    if hits <= 0:
        return 0.0
    span = max_r - min_r
    if hits == 1:
        lo, hi = min_r, min_r + span / 3
    elif hits == 2:
        lo, hi = min_r + span / 3, min_r + span * 2 / 3
    else:  # hits >= 3 (all taps hit a diamond)
        lo, hi = min_r + span * 2 / 3, max_r
    if hi < lo:
        hi = lo
    reward = random.uniform(lo, hi)
    return round(min(max(reward, min_r), max_r), 4)


def _today_range():
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat()
    return now, since


async def _plays_today(db, telegram_id: int, game_type: str) -> int:
    _, since = _today_range()
    cursor = await db.execute(
        "SELECT COUNT(*) as c FROM game_events WHERE telegram_id = ? AND game_type = ? AND created_at >= ?",
        (telegram_id, game_type, since),
    )
    return (await cursor.fetchone())["c"]


async def _last_play_time(db, telegram_id: int, game_type: str):
    cursor = await db.execute(
        "SELECT created_at FROM game_events WHERE telegram_id = ? AND game_type = ? ORDER BY id DESC LIMIT 1",
        (telegram_id, game_type),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    return datetime.fromisoformat(row["created_at"]).replace(tzinfo=timezone.utc)


async def _consume_ad_unlock(db, telegram_id: int, game_type: str, reward_event: str | None):
    """Raises if the extra play isn't legitimately unlocked. Returns True if an ad was used."""
    if not reward_event:
        raise HTTPException(status_code=402, detail="Watch an ad to play again")
    try:
        await db.execute(
            "INSERT INTO game_ad_unlocks (telegram_id, game_type, reward_event) VALUES (?, ?, ?)",
            (telegram_id, game_type, reward_event),
        )
    except Exception:
        raise HTTPException(status_code=409, detail="This ad view was already used")
    return True


async def _credit(db, telegram_id: int, game_type: str, reward: float, used_ad: bool, meta: dict):
    # Always log the play itself (game_events), since daily-limit counting
    # (_plays_today) depends on this row existing regardless of the outcome.
    await db.execute(
        "INSERT INTO game_events (telegram_id, game_type, amount, used_ad, meta) VALUES (?, ?, ?, ?, ?)",
        (telegram_id, game_type, reward, 1 if used_ad else 0, json.dumps(meta)),
    )
    user_cursor = await db.execute("SELECT balance FROM users WHERE telegram_id = ?", (telegram_id,))
    current_balance = (await user_cursor.fetchone())["balance"]

    # A 0-reward round (e.g. a scratch card with no matching diamonds) must
    # NOT create a "_reward" transaction — the transaction history only shows
    # the type's label (e.g. "Scratch card win"), not the amount context, so
    # logging a 0-amount reward row there would visually read as a win that
    # never happened. Balance is unaffected either way, so just skip both.
    if reward <= 0:
        return current_balance

    new_balance = current_balance + reward
    await db.execute(
        "UPDATE users SET balance = ?, total_earned = total_earned + ? WHERE telegram_id = ?",
        (new_balance, reward, telegram_id),
    )
    await db.execute(
        """INSERT INTO transactions (telegram_id, type, amount, balance_after, meta)
           VALUES (?, ?, ?, ?, ?)""",
        (telegram_id, f"{game_type}_reward", reward, new_balance, json.dumps(meta)),
    )
    return new_balance


# ───────────────────────── Spin Wheel ─────────────────────────

@router.get("/spin/status")
async def spin_status(user: dict = Depends(get_current_user)):
    async with get_db() as db:
        cfg = await get_settings(db)
        played_today = await _plays_today(db, user["telegram_id"], "spin")
        last_play = await _last_play_time(db, user["telegram_id"], "spin")

    free_left = max(0, cfg["spin_daily_free_spins"] - played_today)
    max_reached = cfg["spin_max_daily_spins"] > 0 and played_today >= cfg["spin_max_daily_spins"]
    needs_ad = free_left == 0 and cfg["spin_require_ad_after_free"] and not max_reached

    cooldown_remaining = 0
    if last_play:
        elapsed = (datetime.now(timezone.utc) - last_play).total_seconds()
        cooldown_remaining = max(0, cfg["spin_cooldown_seconds"] - int(elapsed))

    return {
        "enabled": cfg["spin_enabled"],
        "segments": cfg["spin_segments"],
        "min_reward": cfg["spin_min_reward"],
        "max_reward": cfg["spin_max_reward"],
        "played_today": played_today,
        "free_spins_left": free_left,
        "max_daily_spins": cfg["spin_max_daily_spins"],
        "max_reached": max_reached,
        "needs_ad": needs_ad,
        "cooldown_remaining": cooldown_remaining,
    }


@router.post("/spin/play")
async def spin_play(payload: GamePlayPayload, user: dict = Depends(get_current_user)):
    telegram_id = user["telegram_id"]
    async with get_db() as db:
        cfg = await get_settings(db)
        if not cfg["spin_enabled"]:
            raise HTTPException(status_code=403, detail="Spin game is currently disabled")

        played_today = await _plays_today(db, telegram_id, "spin")
        if cfg["spin_max_daily_spins"] > 0 and played_today >= cfg["spin_max_daily_spins"]:
            raise HTTPException(status_code=429, detail="Daily spin limit reached, come back tomorrow")

        last_play = await _last_play_time(db, telegram_id, "spin")
        if last_play and (datetime.now(timezone.utc) - last_play).total_seconds() < cfg["spin_cooldown_seconds"]:
            raise HTTPException(status_code=429, detail="Please wait a moment before spinning again")

        used_ad = False
        if played_today >= cfg["spin_daily_free_spins"]:
            if cfg["spin_require_ad_after_free"]:
                used_ad = await _consume_ad_unlock(db, telegram_id, "spin", payload.ad_reward_event)
            # else: unlimited free spins beyond the guaranteed minimum, gated only by cooldown/cap

        # Only segments whose number falls inside the admin's [min, max] range
        # are eligible to actually be landed on — this is what stops the wheel
        # from ever paying out an uncontrolled amount. Segments outside the
        # range still render on the wheel (decorative) but can never win.
        segments = cfg["spin_segments"] or [0]
        min_r, max_r = cfg["spin_min_reward"], cfg["spin_max_reward"]
        if max_r < min_r:
            min_r, max_r = max_r, min_r
        eligible = [i for i, v in enumerate(segments) if min_r <= v <= max_r]
        if not eligible:
            # Misconfigured by the admin (no segment inside the range) — fail safe
            # rather than silently paying an out-of-range amount.
            raise HTTPException(
                status_code=500,
                detail="Spin is misconfigured: no wheel segment falls inside the admin's reward range",
            )
        landed_index = random.choice(eligible)
        reward = round(float(segments[landed_index]), 4)

        new_balance = await _credit(
            db, telegram_id, "spin", reward, used_ad, {"landed_index": landed_index}
        )
        await db.commit()

    return {
        "reward": reward,
        "new_balance": round(new_balance, 4),
        "landed_index": landed_index,
        "segments": segments,
    }


# ───────────────────────── Scratch Card ─────────────────────────

@router.get("/scratch/status")
async def scratch_status(user: dict = Depends(get_current_user)):
    async with get_db() as db:
        cfg = await get_settings(db)
        played_today = await _plays_today(db, user["telegram_id"], "scratch")

    free_left = max(0, cfg["scratch_daily_free"] - played_today)
    max_reached = cfg["scratch_max_daily"] > 0 and played_today >= cfg["scratch_max_daily"]
    needs_ad = free_left == 0 and cfg["scratch_require_ad_after_free"] and not max_reached

    return {
        "enabled": cfg["scratch_enabled"],
        "played_today": played_today,
        "free_plays_left": free_left,
        "max_daily": cfg["scratch_max_daily"],
        "max_reached": max_reached,
        "needs_ad": needs_ad,
        "taps_allowed": SCRATCH_TAPS_ALLOWED,
        "winning_cells_needed": cfg["scratch_winning_cells"],
    }


@router.post("/scratch/play")
async def scratch_play(payload: GamePlayPayload, user: dict = Depends(get_current_user)):
    telegram_id = user["telegram_id"]

    # Must tap exactly SCRATCH_TAPS_ALLOWED distinct cells out of the 9 on the board.
    cells = payload.cells or []
    if len(set(cells)) != SCRATCH_TAPS_ALLOWED or any(c < 0 or c > 8 for c in cells):
        raise HTTPException(
            status_code=400,
            detail=f"Pick exactly {SCRATCH_TAPS_ALLOWED} distinct cells (0-8) to play",
        )
    tapped = set(cells)

    async with get_db() as db:
        cfg = await get_settings(db)
        if not cfg["scratch_enabled"]:
            raise HTTPException(status_code=403, detail="Scratch card is currently disabled")

        played_today = await _plays_today(db, telegram_id, "scratch")
        if cfg["scratch_max_daily"] > 0 and played_today >= cfg["scratch_max_daily"]:
            raise HTTPException(status_code=429, detail="Daily scratch limit reached, come back tomorrow")

        used_ad = False
        if played_today >= cfg["scratch_daily_free"]:
            if cfg["scratch_require_ad_after_free"]:
                used_ad = await _consume_ad_unlock(db, telegram_id, "scratch", payload.ad_reward_event)

        # Diamonds are placed at random AFTER the player has already committed to
        # their 3 cells, so the result is genuinely random each round and can't
        # be predicted from past rounds — the positions always shuffle.
        winning_count = max(1, min(9, cfg["scratch_winning_cells"]))
        diamond_cells = set(random.sample(range(9), winning_count))

        hits = len(tapped & diamond_cells)
        min_r, max_r = cfg["scratch_min_reward"], cfg["scratch_max_reward"]
        reward = _scratch_reward_for_hits(hits, min_r, max_r)

        new_balance = await _credit(
            db, telegram_id, "scratch", reward, used_ad,
            {"tapped_cells": sorted(tapped), "diamond_cells": sorted(diamond_cells), "hits": hits},
        )
        await db.commit()

    return {
        "reward": reward,
        "new_balance": round(new_balance, 4),
        "tapped_cells": sorted(tapped),
        "diamond_cells": sorted(diamond_cells),
        "matched_cells": sorted(tapped & diamond_cells),
        "hits": hits,
    }
