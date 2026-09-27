import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    app_name: str = "Schreduler"
    environment: str = os.getenv("ENVIRONMENT", "development")
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./schreduler.db")
    llm_api_key: str | None = os.getenv("LLM_API_KEY")
    llm_model: str = os.getenv("LLM_MODEL", "gpt-5.6-luna")
    # 일정 어시스턴트(Responses API) 전용. 페르소나·체크인·미준수 피드백은 계속 llm_model을 쓴다.
    assistant_model: str = os.getenv("ASSISTANT_MODEL") or llm_model
    assistant_reasoning_effort: str = os.getenv("ASSISTANT_REASONING_EFFORT", "low")
    # 날짜·시각 해석의 기준 시간대 (IANA 이름). 사용자별 시간대가 생기기 전까지 서버 전체에 하나.
    app_timezone: str = os.getenv("APP_TIMEZONE", "America/Toronto")
    # Firebase 서비스 계정 JSON 파일 경로. 미설정이면 FCM 발송은 스킵되고 로그만 남는다.
    firebase_credentials_path: str | None = os.getenv("FIREBASE_CREDENTIALS_PATH")
    # 텔레그램 봇 토큰 (BotFather 발급). 미설정이면 텔레그램 발송은 스킵되고 로그만 남는다.
    telegram_bot_token: str | None = os.getenv("TELEGRAM_BOT_TOKEN")


settings = Settings()
