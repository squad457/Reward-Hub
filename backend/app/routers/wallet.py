from fastapi import APIRouter, Depends, HTTPException
from app.auth import get_current_user
from app.config import settings
from app.database import get_db

router = APIRouter(prefix="/api/wallet", tags=["wallet"])

@router.post("/withdraw")
async def withdraw(user: dict = Depends(get_current_user)):
    min_withdrawal = float(settings.MIN_WITHDRAWAL_USDT or 10.0)
    if user["balance"] < min_withdrawal:
        raise HTTPException(status_code=400, detail=f"Balance insufficient. Minimum withdrawal is ${min_withdrawal}")
    # Withdrawal logic placeholder
    return {"amount": user["balance"], "status": "pending"}