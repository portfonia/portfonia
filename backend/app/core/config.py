"""Typed configuration loaded from .env.local (dev) or process env (prod)."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root contains .env.local (one level above backend/)
PROJECT_ROOT = Path(__file__).resolve().parents[3]

# OpenRouter attribution headers, so calls show up tagged "Portfonia" in the
# OpenRouter dashboard/logs instead of unattributed. HTTP-Referer MUST be a
# valid URL (https://openrouter.ai/docs/app-attribution) — a bare string is
# silently dropped by OpenRouter, which also disables X-OpenRouter-Title.
# Merge into every OpenRouter client/request's headers via
# **OR_ATTRIBUTION_HEADERS — never hardcode these as literals at a call site.
OR_ATTRIBUTION_HEADERS: dict[str, str] = {
    "HTTP-Referer": "https://github.com/portfonia/portfonia",
    "X-OpenRouter-Title": "Portfonia",
}


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
    # Pass 2 report body + regenerate ONLY. Unaffected by the BYOK/structured
    # split below (issue #78) — stays on OPENROUTER_PROVIDER_ORDER (marketplace
    # pin) with OPENROUTER_DATA_COLLECTION=deny enforced, no exception.
    PRIMARY_LLM_MODEL: str
    # Unstructured (free-text) calls only: Pass 1 search-query generation and
    # report translation (report_generator.py). Structured/JSON extraction
    # (holdings parsing) does NOT use this — see STRUCTURED_LLM_MODEL below.
    # These two call sites route via OpenRouter BYOK (order=["DeepSeek"],
    # allow_fallbacks forced False — no marketplace fallback) straight to
    # DeepSeek's own backend and run WITHOUT the OPENROUTER_DATA_COLLECTION
    # guard below (issue #78, decision 2026-08-06: accepted as a scoped compliance
    # exception for these two calls only — everything else keeps "deny"). The
    # hard pin (no fallback) matters here specifically because translation
    # carries holdings-derived report text: if DeepSeek's endpoint is
    # unavailable, the call must fail rather than silently reroute that payload
    # to an arbitrary marketplace provider the deny guard would otherwise have
    # excluded (PR #79 review).
    LOW_COST_LLM_MODEL: str
    # Provider pinning for Pass 2 / regenerate (PRIMARY_LLM_MODEL) — precision/
    # quality for report synthesis, not a structured-extraction concern (that's
    # STRUCTURED_LLM_MODEL + structured_provider below). OpenRouter routes one
    # model id across providers of differing precision; for deepseek/deepseek-
    # v4-pro pin "DigitalOcean,Venice" (high precision). See Daily_Intel design
    # doc section 8. Comma-separated, highest priority first. Empty = no order
    # pin.
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
    # NOT applied to Pass 1 / translation — see LOW_COST_LLM_MODEL comment above.
    OPENROUTER_DATA_COLLECTION: str = "deny"
    # The only model used for structured (JSON schema-required) extraction —
    # currently holdings parsing only. Uniformly routed here (not a fallback tier
    # under LOW_COST_LLM_MODEL any more — issue #78).
    #
    # Was google/gemma-4-31b-it pinned to the OpenInference bf16 endpoint
    # (verified at 100% / 210/210 on the PC611-homepage eval case set — see
    # the PC611-homepage project's "LLM No-Reasoning Eval" design/implementation
    # notes §19-21) until issue
    # #84 (2026-08-06): direct production probing found the OpenInference
    # bf16 endpoint itself was the bottleneck (371s worst case on a 30-row
    # holdings file — pinning it made every variant *slower*, not more
    # accurate-per-second), causing real uploads to blow through the Celery
    # task's time_limit and get SIGKILLed before ever writing a result.
    # openai/gpt-5.6-luna (routed through OpenAI's own infra, not a
    # third-party quantized marketplace reseller — no equivalent
    # precision-pin concern) measured 10.9-13.8s on the same file with a
    # full manual accuracy audit (30/30 rows correct), with
    # reasoning_effort=none (app/services/holding_parser.py —
    # _STRUCTURED_REASONING_EFFORT; provider routing itself is
    # app/core/llm.py:structured_provider, which does not touch reasoning).
    # One manual run, not yet a systematic eval on the scale that qualified
    # the previous model — worth a broader pass before treating this as
    # fully validated long-term.
    STRUCTURED_LLM_MODEL: str = "openai/gpt-5.6-luna"

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
    # Ops alert recipient — receives failure/needs_review notifications.
    ADMIN_EMAIL: str = "portfonia@gmail.com"

    # GitHub issue creation for bug tracking. Optional: when unset, issue
    # creation is skipped silently. Token needs repo scope (issues:write).
    GITHUB_TOKEN: SecretStr | None = None
    GITHUB_REPO: str = "portfonia/portfonia"

    # Ring 0 dev identity
    DEV_USER_ID: str
    DEV_USER_EMAIL: str

    # Macro keyword config
    # Empty string = use the default path: backend/config/macro_keywords.yml
    # Override via .env.local to point at a different file.
    MACRO_KEYWORDS_PATH: str = ""

    # Holding-relevant news recall config (R-3)
    # Empty string = use the default path: backend/config/holding_news_keywords.yml
    HOLDING_NEWS_KEYWORDS_PATH: str = ""

    # asset_class anomaly + concentration thresholds (admin-editable, #35)
    # Empty string = use the default path: backend/config/asset_class_thresholds.yml
    ASSET_CLASS_CONFIG_PATH: str = ""

    # Locale-keyed glossary of fixed non-English terms (#90)
    # Empty string = use the default path: backend/config/i18n_glossary.yml
    I18N_GLOSSARY_PATH: str = ""

    # Chinese-language compliance vocabulary data (#90)
    # Empty string = use the default path: backend/config/compliance_vocab.yml
    COMPLIANCE_VOCAB_PATH: str = ""

    # Chinese-language example/vocabulary data for the holdings-extraction prompt (#90)
    # Empty string = use the default path: backend/config/holding_parser_vocab.yml
    HOLDING_PARSER_VOCAB_PATH: str = ""

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
