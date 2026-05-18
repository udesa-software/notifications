from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql://postgres:postgres@notifications-db:5432/notifications"
    FIREBASE_SERVICE_ACCOUNT_PATH: Optional[str] = "firebase-credentials.json"
    INTERNAL_SECRET: str = "fca7y49uhe9r84fh0eah0f8HB08hHCIH4S904F"
    PORT: int = 8080

    class Config:
        env_file = ".env"

settings = Settings()
