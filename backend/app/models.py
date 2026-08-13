from pydantic import BaseModel
from typing import Optional, List

class GamePlayPayload(BaseModel):
    reward_event: Optional[str] = None
    cells: Optional[List[int]] = None

class AdRewardPayload(BaseModel):
    reward_event: str