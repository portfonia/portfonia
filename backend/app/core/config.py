"""Typed configuration loaded from .env.local (dev) or process env (prod)."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root contains .env.local (one level above backend/)
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env.local",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # App
    APP_ENV: str = "development"
    APP_SECRET_KEY: SecretStr
    APP_BASE_URL: str
    FRONTEND_URL: str

    # Database
    DB_HOST: str
    DB_PORT: int = 5432
    DB_NAME: str
    DB_USER: str
    DB_PASSWORD: SecretStr

    # Redis
    REDIS_HOST: str
    REDIS_PORT: int = 6379

    # LLM (OpenRouter)
    LLM_PROVIDER: str = "openrouter"
    OPENROUTER_API_KEY: SecretStr
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    PRIMARY_LLM_MODEL: str
    LOW_COST_LLM_MODEL: str
    # Provider pinning for structured extraction. OpenRouter routes one model id
    # across providers of differing precision; aggressive-quantization resellers
    # (NovitaAI, StreamLake) degrade JSON/schema compliance and must never be used
    # for structured extraction. Portfonia runs low-cost open models (e.g.
    # deepseek/deepseek-v4-flash) for cost reasons, not Claude; for V4 Flash pin
    # "DigitalOcean,Venice" (high precision). See Daily_Intel design doc section 8.
    # Comma-separated, highest priority first. Empty = no order pin.
    OPENROUTER_PROVIDER_ORDER: str = ""
    OPENROUTER_ALLOW_FALLBACKS: bool = True
    # Data-collection policy for OpenRouter provider routing. Portfonia is a
    # multi-tenant SaaS handling user holdings, so any call carrying holdings data
    # (parsing, personalized reports, follow-ups) must route ONLY to providers
    # that do not retain or train on the payload. "deny" makes OpenRouter exclude
    # data-collecting providers — this is how we use cheap DeepSeek V4 Flash/Pro
    # while still meeting the "not used for training" requirement (we never hit the
    # DeepSeek first-party API, whose terms allow training). See §8.8. Set to empty
    # only to disable this guard (not recommended for holdings-bearing calls).
    OPENROUTER_DATA_COLLECTION: str = "deny"

    # Report output language. The LLM reasons in English (higher quality), then
    # the assembled report is translated to this language at render time. "en"
    # skips translation. Ring 0 default: Simplified Chinese.
    OUTPUT_LANG: str = "zh"

    # Search
    TAVILY_API_KEY: SecretStr
    TAVILY_DAILY_BUDGET: int = 10

    # Forward calendar (#1). Optional: when unset, the macro release dates from
    # FRED are skipped and the forward block falls back to FOMC + earnings only.
    FRED_API_KEY: SecretStr | None = None

    # Email
    EMAIL_PROVIDER: str = "resend"
    RESEND_API_KEY: SecretStr
    EMAIL_FROM: str
    EMAIL_REPLY_TO: str

    # Ring 0 dev identity
    DEV_USER_ID: str
    DEV_USER_EMAIL: str

    # Macro keyword config
    # Empty string = use the default path: backend/config/macro_keywords.yml
    # Override via .env.local to point at a different file.
    MACRO_KEYWORDS_PATH: str = ""

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.DB_USER}:{self.DB_PASSWORD.get_secret_value()}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
