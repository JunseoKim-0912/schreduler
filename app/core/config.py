import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    app_name: str = "Schreduler"
    environment: str = os.getenv("ENVIRONMENT", "development")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./schreduler.db")
    llm_api_key: str | None = os.getenv("LLM_API_KEY")
    llm_model: str = os.getenv("LLM_MODEL", "gpt-4o-mini")


settings = Settings()
