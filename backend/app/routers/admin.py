from fastapi import APIRouter, Depends
from app.auth import verify_admin
from app.database import get_db

router = APIRouter(prefix="/api/admin", tags=["admin"])

@router.get("/stats")
async def admin_stats(_: bool = Depends(verify_admin)):
    async with get_db() as db:
        cursor = await db.execute("SELECT COUNT(*) as total FROM users")
        total_users = (await cursor.fetchone())["total"]
        return {"total_users": total_users}