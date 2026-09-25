import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    app_name: str = "Schreduler"
    environment: str = os.getenv("ENVIRONMENT", "development")
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./schreduler.db")
    llm_api_key: str | None = os.getenv("LLM_API_KEY")
    llm_model: str = os.getenv("LLM_MODEL", "gpt-4o-mini")
    # Firebase 서비스 계정 JSON 파일 경로. 미설정이면 FCM 발송은 스킵되고 로그만 남는다.
    firebase_credentials_path: str | None = os.getenv("FIREBASE_CREDENTIALS_PATH")
    # 텔레그램 봇 토큰 (BotFather 발급). 미설정이면 텔레그램 발송은 스킵되고 로그만 남는다.
    telegram_bot_token: str | None = os.getenv("TELEGRAM_BOT_TOKEN")


settings = Settings()
