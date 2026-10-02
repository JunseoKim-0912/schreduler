import os

from dotenv import load_dotenv

load_dotenv()


def _flag(name: str, *, default: bool) -> bool:
    value = (os.getenv(name) or "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


class Settings:
    app_name: str = "Schreduler"
    environment: str = os.getenv("ENVIRONMENT", "development")
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./schreduler.db")
    llm_api_key: str | None = os.getenv("LLM_API_KEY")
    llm_model: str = os.getenv("LLM_MODEL", "gpt-5.6-luna")
    # 일정 어시스턴트(Responses API) 전용. 페르소나·체크인·미준수 피드백은 계속 llm_model을 쓴다.
    # 기본값은 C단계 평가(docs/assistant_design.md 11.7)로 정했다 — LLM_MODEL과 따로 간다.
    assistant_model: str = os.getenv("ASSISTANT_MODEL") or "gpt-5.6-luna"
    assistant_reasoning_effort: str = os.getenv("ASSISTANT_REASONING_EFFORT") or "medium"
    # 날짜·시각 해석의 기준 시간대 (IANA 이름). 사용자별 시간대가 생기기 전까지 서버 전체에 하나.
    app_timezone: str = os.getenv("APP_TIMEZONE", "America/Toronto")
    # Daily LLM spend caps in USD, reset at APP_TIMEZONE midnight. The admin cap applies to users.is_admin only when set.
    llm_daily_budget_per_user_usd: float = float(os.getenv("LLM_DAILY_BUDGET_PER_USER_USD") or "1.00")
    llm_daily_budget_total_usd: float = float(os.getenv("LLM_DAILY_BUDGET_TOTAL_USD") or "5.00")
    llm_daily_budget_admin_usd: float | None = (
        float(os.environ["LLM_DAILY_BUDGET_ADMIN_USD"]) if os.getenv("LLM_DAILY_BUDGET_ADMIN_USD") else None
    )
    # Sign-in. SIGNUP_MODE: closed (nobody can sign up) / invite (needs INVITE_CODE) / open.
    signup_mode: str = (os.getenv("SIGNUP_MODE") or "invite").strip().lower()
    invite_code: str | None = os.getenv("INVITE_CODE") or None
    # Turn on behind HTTPS so the session cookie is never sent over plain HTTP.
    session_cookie_secure: bool = _flag("SESSION_COOKIE_SECURE", default=False)
    # Comma-separated origins allowed to send POST/PUT/DELETE (e.g. https://schreduler.example.com).
    # Empty: same host as the request.
    allowed_origins: tuple[str, ...] = tuple(
        o.strip().rstrip("/").lower() for o in (os.getenv("ALLOWED_ORIGINS") or "").split(",") if o.strip()
    )
    # Firebase 서비스 계정 JSON 파일 경로. 미설정이면 FCM 발송은 스킵되고 로그만 남는다.
    firebase_credentials_path: str | None = os.getenv("FIREBASE_CREDENTIALS_PATH")
    # Same service account as JSON text, so the file never has to be baked into an image. Wins over the path.
    firebase_credentials_json: str | None = os.getenv("FIREBASE_CREDENTIALS_JSON") or None
    # The scheduler runs inside the server process; turn it off for one-off containers (or a second replica, which
    # must not exist: two schedulers send every notification twice).
    run_scheduler: bool = _flag("RUN_SCHEDULER", default=True)
    # Behind a proxy every request comes from the proxy's address. Only when this is on is the client IP taken from
    # X-Forwarded-For and used for the per-IP sign-in lockout; off, the lockout counts per email only.
    trust_proxy_headers: bool = _flag("TRUST_PROXY_HEADERS", default=False)
    # Daily SQLite backups. Empty: a "backups" folder next to the database file.
    backup_dir: str | None = os.getenv("BACKUP_DIR") or None
    # 텔레그램 봇 토큰 (BotFather 발급). 미설정이면 텔레그램 발송은 스킵되고 로그만 남는다.
    telegram_bot_token: str | None = os.getenv("TELEGRAM_BOT_TOKEN")


settings = Settings()
