from pydantic import BaseModel
from typing import Optional

class TokenRegistration(BaseModel):
    user_id: str
    fcm_token: str

class NotificationRequest(BaseModel):
    user_id: str
    title: str
    body: str
    data: Optional[dict] = None
