from pydantic import BaseModel
from typing import Optional, Dict, Any

class TokenRegistration(BaseModel):
    user_id: Optional[str] = None
    fcm_token: str

class NotificationRequest(BaseModel):
    user_id: str
    title: str
    body: str
    data: Optional[Dict[str, Any]] = None
