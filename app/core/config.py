import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    app_name: str = "Schreduler"
    environment: str = os.getenv("ENVIRONMENT", "development")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./schreduler.db")


settings = Settings()
