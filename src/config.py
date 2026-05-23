from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    DB_HOST: str = "notifications-db"
    DB_PORT: int = 5432
    DB_NAME: str = "notifications"
    DB_USER: str = "postgres"
    DB_PASSWORD: str = "postgres"
    
    FIREBASE_SERVICE_ACCOUNT_PATH: Optional[str] = "firebase-credentials.json"
    INTERNAL_SECRET: str = "fca7y49uhe9r84fh0eah0f8HB08hHCIH4S904F"
    PORT: int = 8080

    @property
    def DATABASE_URL(self) -> str:
        return f"postgresql://{self.DB_USER}:{self.DB_PASSWORD}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    class Config:
        env_file = ".env"

settings = Settings()

