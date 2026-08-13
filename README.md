# Reward Hub — Telegram Mini App

Earn-gems Mini App: Daily Rewards streak, admin-controlled Spin Wheel, Tasks,
AdsGram rewarded ads, Referrals, Gift Codes, VIP flag, USDT withdrawal.
Stack: FastAPI + aiosqlite (Railway) · aiogram bot · HTML/JS frontend (Vercel).

## Structure
```
gemcraft/
  backend/
    database.py     # schema + data access (wheel_segments, spin_config, gift_codes, withdrawals...)
    main.py          # FastAPI API incl. /api/admin/* (deploy to Railway)
    bot.py            # aiogram bot (deploy to Railway as a second service, or same one)
    requirements.txt
  frontend/
    index.html         # Mini App UI (deploy to Vercel)
    admin.html          # Admin dashboard — spin range, wheel segments, tasks, gift codes, withdrawals
```

## How the spin control works
- Admin sets `min_reward` / `max_reward` in the dashboard.
- Each wheel segment is flagged `is_real` (payable) or decorative.
- A spin is decided by picking a random segment from those that are BOTH `is_real=1`
  AND whose value already sits inside `[min_reward, max_reward]`.
- The wheel's spin animation is calculated from that exact winning segment's position,
  so the visible landing spot and the credited reward are always the same number —
  this fixes the "lands on X but credits Y" bug from the Noal build.
- Segments outside the range (e.g. 1, 2, 5000, 10000 in the seed data) stay visible on
  the wheel for excitement but can never be selected as the outcome. Seed data ships
  with 4 decorative segments and 4 real ones — adjust freely in the admin dashboard.
- If you tighten the range so no segment qualifies, the dashboard warns you and spins
  will fail until you add an eligible segment.

## Setup

1. **Backend (Railway)**
   - `pip install -r backend/requirements.txt`
   - Env vars: `BOT_TOKEN`, `WEBAPP_URL` (Vercel URL), `ADMIN_KEY` (your own secret string —
     inserted into the `admins` table on first boot; add more keys directly in that table)
   - Run API: `uvicorn main:app --host 0.0.0.0 --port $PORT` (from `backend/`)
   - Run bot as a separate process/service: `python bot.py`

2. **Frontend (Vercel)**
   - Deploy `frontend/` as a static site
   - In `index.html` and `admin.html`: set `API_BASE` (the only value that still lives in code —
     everything else below is configured live from the admin dashboard)
   - Restrict `admin.html` (e.g. separate subdomain, not linked from the bot) since it only
     has a client-side key gate — the real protection is the `X-Admin-Key` check on the API.

3. **Telegram**
   - Create the bot with @BotFather as **Reward Hub**, enable Mini App, set the Web App URL
   - `/start` supports referral deep-links: `t.me/YourBot?start=<referral_code>` (+1 bonus spin per invite)

4. **AdsGram — configured entirely from the admin dashboard**
   - Register the Mini App on the AdsGram partner dashboard, get a Block ID
   - Open `admin.html` → **App Settings** → paste the Block ID, your bot's username (no `@`),
     and (optionally) the app display name and gems→USDT rate → Save
   - The Mini App fetches these from `GET /api/settings` on load and initializes AdsGram
     with them — no redeploy needed to rotate the Block ID or rename the bot
   - `index.html` calls AdsGram for: daily bonus gems, bonus spins
   - `/api/ads/reward` is where you tune payouts per placement (`daily_bonus`, `spin_bonus`)

## Still open (flag before going live)
- Withdrawals are stored as `pending` and paid out manually via the admin dashboard —
  no on-chain/exchange payout automation is wired up
- No anti-abuse checks (VPN/fingerprint/clone/multi-account detection) — worth porting
  from your HF v4 project before this handles real payouts
- Gems→USDT rate is now admin-editable (App Settings) — defaults to 1000 gems = $1
