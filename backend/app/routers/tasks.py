from fastapi import APIRouter, Depends, HTTPException
from app.auth import get_current_user
from app.database import get_db

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

@router.get("")
async def get_tasks(user: dict = Depends(get_current_user)):
    async with get_db() as db:
        cursor = await db.execute("SELECT id, title, description, reward FROM tasks WHERE is_active = 1")
        tasks = [dict(row) for row in await cursor.fetchall()]
        return tasks

@router.post("/{task_id}/claim")
async def claim_task(task_id: int, user: dict = Depends(get_current_user)):
    async with get_db() as db:
        cursor = await db.execute("SELECT reward FROM tasks WHERE id = ? AND is_active = 1", (task_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Task not found")
        reward = row["reward"]
        new_balance = user["balance"] + reward
        await db.execute(
            "UPDATE users SET balance = ?, total_earned = total_earned + ? WHERE telegram_id = ?",
            (new_balance, reward, user["telegram_id"])
        )
        await db.commit()
    return {"reward": reward}