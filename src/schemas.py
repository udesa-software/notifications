from pydantic import BaseModel
from typing import Optional, Dict, Any, List
from datetime import datetime

class TokenRegistration(BaseModel):
    user_id: Optional[str] = None
    fcm_token: str

class NotificationRequest(BaseModel):
    user_id: str
    title: str
    body: str
    data: Optional[Dict[str, Any]] = None

class NotificationResponse(BaseModel):
    id: int
    user_id: str
    title: str
    body: str
    data: Optional[Dict[str, Any]] = None
    is_read: bool
    is_deleted: bool
    created_at: datetime

    class Config:
        orm_mode = True

class PaginatedNotifications(BaseModel):
    notifications: List[NotificationResponse]
    total: int
    page: int
    pages: int
    per_page: int

