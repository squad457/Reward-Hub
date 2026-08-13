from pydantic import BaseModel
from typing import Optional, List

class GamePlayPayload(BaseModel):
    reward_event: Optional[str] = None
    cells: Optional[List[int]] = None

class AdRewardPayload(BaseModel):
    reward_event: str

class WithdrawPayload(BaseModel):
    method: str
    address: str

class TaskCreatePayload(BaseModel):
    title: str
    description: Optional[str] = None
    url: str
    reward: float
    task_type: str = "link"

class SettingsUpdatePayload(BaseModel):
    values: dict