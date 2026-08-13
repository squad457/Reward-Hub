import json

from fastapi import APIRouter, Depends, HTTPException
from app.auth import get_current_user
from app.database import get_db, credit_referral_commission

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("")
async def get_tasks(user: dict = Depends(get_current_user)):
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT t.id, t.title, t.description, t.url, t.reward, t.task_type,
                      ut.telegram_id IS NOT NULL AS completed
               FROM tasks t
               LEFT JOIN user_tasks ut
                      ON ut.task_id = t.id AND ut.telegram_id = ?
               WHERE t.is_active = 1
               ORDER BY t.id""",
            (user["telegram_id"],),
        )
        tasks = [dict(row) for row in await cursor.fetchall()]
        for t in tasks:
            t["completed"] = bool(t["completed"])
        return tasks


@router.post("/{task_id}/claim")
async def claim_task(task_id: int, user: dict = Depends(get_current_user)):
    telegram_id = user["telegram_id"]
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT reward FROM tasks WHERE id = ? AND is_active = 1", (task_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        reward = row["reward"]

        # Record the completion first — the UNIQUE(telegram_id, task_id) constraint
        # on user_tasks is what stops the same task being claimed twice. Without
        # this insert (the previous version never wrote to user_tasks at all) a
        # user could tap "Claim" on the same task endlessly for infinite reward.
        try:
            await db.execute(
                "INSERT INTO user_tasks (telegram_id, task_id, status) VALUES (?, ?, 'completed')",
                (telegram_id, task_id),
            )
        except Exception:
            raise HTTPException(status_code=409, detail="Task already claimed")

        user_cursor = await db.execute(
            "SELECT balance FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        current_balance = (await user_cursor.fetchone())["balance"]
        new_balance = current_balance + reward

        await db.execute(
            "UPDATE users SET balance = ?, total_earned = total_earned + ? WHERE telegram_id = ?",
            (new_balance, reward, telegram_id),
        )
        await db.execute(
            """INSERT INTO transactions (telegram_id, type, amount, balance_after, meta)
               VALUES (?, 'task_reward', ?, ?, ?)""",
            (telegram_id, reward, new_balance, json.dumps({"task_id": task_id})),
        )
        await credit_referral_commission(db, telegram_id, reward)
        await db.commit()
    return {"reward": reward, "new_balance": round(new_balance, 4)}
