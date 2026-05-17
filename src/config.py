from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql://postgres:postgres@notifications-db:5432/notifications"
    FIREBASE_SERVICE_ACCOUNT_PATH: Optional[str] = None
    INTERNAL_SECRET: str = "your-internal-secret-here"
    PORT: int = 8080

    class Config:
        env_file = ".env"

settings = Settings()
