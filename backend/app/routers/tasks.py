import json

from fastapi import APIRouter, Depends, HTTPException

from app.auth import get_current_user
from app.database import get_db
from app.models import TaskCompletePayload

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("")
async def list_tasks(user: dict = Depends(get_current_user)):
    """Returns active tasks with a `completed` flag per the requesting user."""
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM tasks WHERE is_active = 1 ORDER BY id DESC")
        tasks = [dict(r) for r in await cursor.fetchall()]

        done_cursor = await db.execute(
            "SELECT task_id FROM user_tasks WHERE telegram_id = ?", (user["telegram_id"],)
        )
        completed_ids = {r["task_id"] for r in await done_cursor.fetchall()}

    for t in tasks:
        t["completed"] = t["id"] in completed_ids
    return tasks


@router.post("/complete")
async def complete_task(payload: TaskCompletePayload, user: dict = Depends(get_current_user)):
    async with get_db() as db:
        task_cursor = await db.execute(
            "SELECT * FROM tasks WHERE id = ? AND is_active = 1", (payload.task_id,)
        )
        task = await task_cursor.fetchone()
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        try:
            await db.execute(
                "INSERT INTO user_tasks (telegram_id, task_id) VALUES (?, ?)",
                (user["telegram_id"], payload.task_id),
            )
        except Exception:
            raise HTTPException(status_code=409, detail="Task already completed")

        new_balance = user["balance"] + task["reward"]
        await db.execute(
            "UPDATE users SET balance = ?, total_earned = total_earned + ? WHERE telegram_id = ?",
            (new_balance, task["reward"], user["telegram_id"]),
        )
        await db.execute(
            """INSERT INTO transactions (telegram_id, type, amount, balance_after, meta)
               VALUES (?, 'task_reward', ?, ?, ?)""",
            (user["telegram_id"], task["reward"], new_balance, json.dumps({"task_id": task["id"]})),
        )
        await db.commit()

    return {"reward": task["reward"], "new_balance": round(new_balance, 4)}
