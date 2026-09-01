"""Typed configuration loaded from .env.local (dev) or process env (prod)."""

from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import AliasChoices, Field, SecretStr, field_validator
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


def _require_fernet_key(v: SecretStr) -> SecretStr:
    """Shared check for the two HOLDINGS_ENCRYPTION_KEY* validators below —
    v must decode as a well-formed Fernet key. Callers decide separately
    whether a blank value is an error or means "unset"."""
    try:
        Fernet(v.get_secret_value().encode())
    except ValueError as exc:
        raise ValueError(
            "must be a valid Fernet key (Fernet.generate_key(), 44-char "
            "url-safe base64) — see app/core/encryption.py"
        ) from exc
    return v


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

    # --- Shared-compute personalized assembly (issue #128 A4) ---------------
    # Master switch for the second-layer assembly path (design doc §6.3). When
    # false — the default, and the only value in production until the shadow
    # comparison below has been read and signed off — generate_report behaves
    # exactly as it did before A4: one PRIMARY_LLM_MODEL Pass 2 call writes the
    # whole body. When true, the body is assembled from the pre-computed L1/L2
    # shared intel instead, falling back to Pass 2 whenever that intel is
    # missing or the assembled body fails the same completeness guard. The
    # worst case of enabling it is therefore "today's behavior", never a
    # degraded report.
    SHARED_COMPUTE_ENABLED: bool = False
    # Model for the assembly pass. Carries holdings (portfolio weights), so it
    # runs with OPENROUTER_DATA_COLLECTION=deny ENFORCED and does NOT use the
    # BYOK exception scoped to Pass 1 + translation (design doc §6.3). Left
    # empty deliberately: the value is an OUTPUT of the shadow comparison, not
    # an input to it (decision 2026-08-14 — the assembly task shape differs
    # from Pass 1/translation, so a cheap model's quality here cannot be
    # assumed). An empty value with SHARED_COMPUTE_ENABLED=true falls back to
    # Pass 2 rather than guessing a model.
    ASSEMBLY_LLM_MODEL: str = ""
    # Shadow comparison harness (design doc §6.3.1). Comma-separated model ids;
    # empty disables it. Each listed model runs the assembly pass over the same
    # inputs as the shipped report and its output is stored under
    # report_inputs["assembly_shadow"] — never rendered, never emailed, never
    # able to fail the report. Run this with SHARED_COMPUTE_ENABLED=false so
    # one round yields BOTH comparisons the design asks for in a single pass:
    # architecture (the shipped Pass 2 body vs each assembled body) and model
    # (the listed models against each other), with costs read straight off
    # report_inputs["llm_calls"].
    ASSEMBLY_SHADOW_MODELS: str = ""

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
    # Separate `full_access`-scoped Resend key (issue #260, Ring 1-Email
    # Validation design doc §3.3 step 6 / §六): RESEND_API_KEY above is
    # `sending_access`-only and cannot call Resend's GET /emails/{id}
    # (confirmed against Resend's own docs — a sending_access key gets a
    # permission error, not the email). Deliberately a separate key, not an
    # upgrade of RESEND_API_KEY — the send path has no reason to hold a key
    # that can read/write arbitrary Resend resources. Optional: when unset,
    # the delivery-status poll task skips silently (no webhook/poll
    # infrastructure required for the verification flow's core click-confirm
    # path to work).
    RESEND_ALL_ACCESS_API_KEY: SecretStr | None = None
    # Ops alert recipient — receives failure/needs_review notifications.
    ADMIN_EMAIL: str = "portfonia@gmail.com"

    # GitHub issue creation for bug tracking. Optional: when unset, issue
    # creation is skipped silently. Token needs repo scope (issues:write).
    GITHUB_TOKEN: SecretStr | None = None
    GITHUB_REPO: str = "portfonia/portfonia"

    # Ring 0 dev identity
    DEV_USER_ID: str
    DEV_USER_EMAIL: str

    # Holdings field-level encryption at rest (issue #31). Fernet key
    # (Fernet.generate_key(), url-safe base64, 44 chars). _PREV is optional —
    # set during key rotation only (see app/core/encryption.py for the
    # rotation mechanics); leave unset otherwise.
    HOLDINGS_ENCRYPTION_KEY: SecretStr
    HOLDINGS_ENCRYPTION_KEY_PREV: SecretStr | None = None

    @field_validator("HOLDINGS_ENCRYPTION_KEY")
    @classmethod
    def _validate_primary_fernet_key(cls, v: SecretStr) -> SecretStr:
        """Required key — blank or malformed must fail at boot.

        Unlike HOLDINGS_ENCRYPTION_KEY_PREV below, a blank value here is NOT
        treated as "unset" — there's no unset state for the required active
        key, so a blank env value (HOLDINGS_ENCRYPTION_KEY=) is a
        misconfiguration and must fail loudly now, not on first holdings
        read via HoldingsDecryptionError (PR #111 re-review — sharing one
        validator with the optional PREV field let a blank primary key
        silently pass settings load).
        """
        return _require_fernet_key(v)

    @field_validator("HOLDINGS_ENCRYPTION_KEY_PREV")
    @classmethod
    def _validate_prev_fernet_key(cls, v: SecretStr | None) -> SecretStr | None:
        """Optional rotation key — a blank env value means unset, not malformed.

        Matches the runtime handling in app/core/encryption.py's
        _build_fernet, which also treats "" as absent.
        """
        if v is None or not v.get_secret_value():
            return v
        return _require_fernet_key(v)

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

    # LLM call retry/backoff tuning (admin-editable, #38)
    # Empty string = use the default path: backend/config/llm_retry.yml
    LLM_RETRY_CONFIG_PATH: str = ""

    # System default analysis framework — house investment-analysis stance
    # injected into every Pass 2 / assembly system prompt (issue #129 Ring 1
    # stage B, checkpoint B1). Empty string = use the default path:
    # backend/config/analysis_framework.yml
    ANALYSIS_FRAMEWORK_CONFIG_PATH: str = ""

    # Chinese-language example/vocabulary data for the holdings-extraction prompt (#90)
    # Empty string = use the default path: backend/config/holding_parser_vocab.yml
    HOLDING_PARSER_VOCAB_PATH: str = ""

    # ticker/fund_code → asset_class mapping (issue #296). Hot-reloadable so an
    # admin can add a real production instrument without a code deploy.
    # Empty string = use the default path: backend/config/ticker_asset_class.yml
    TICKER_ASSET_CLASS_CONFIG_PATH: str = ""

    # Ops API token channel (issue #129 Ring 1 stage B, checkpoint B2) —
    # bearer secret guarding /admin/* routes, deliberately independent of the
    # user auth system (must still work if that system itself is what's
    # broken). Required everywhere, no unset state — a missing value fails
    # Settings load in every environment, same discipline as
    # HOLDINGS_ENCRYPTION_KEY; production's own .env carries its own
    # generated value, never copied from a dev .env.local (Ring 1-B design
    # doc §4.4/§4.7). _PREV is optional, for a no-downtime rotation window —
    # same double-key pattern as HOLDINGS_ENCRYPTION_KEY/_PREV.
    ADMIN_API_TOKEN: SecretStr
    ADMIN_API_TOKEN_PREV: SecretStr | None = None

    # Hosted Auth (Supabase). Required — B4 is the first wiring of these
    # fields; a missing value must fail Settings load, same as
    # HOLDINGS_ENCRYPTION_KEY / ADMIN_API_TOKEN. Verification uses the
    # project's JWKS (ES256/RS256), derived from SUPABASE_URL — there is
    # no JWT_SECRET setting. See Ring 1-B design.md §6.5.
    #
    # Dashboard names (2026): publishable key / secret key. Env aliases
    # accept both the dashboard names and the older anon / service_role
    # names. Do not store the JWT signing secret — we never verify HS256.
    SUPABASE_URL: str
    SUPABASE_ANON_KEY: SecretStr = Field(
        validation_alias=AliasChoices("SUPABASE_ANON_KEY", "SUPABASE_PUBLISHABLE_KEY")
    )
    SUPABASE_SERVICE_ROLE_KEY: SecretStr = Field(
        validation_alias=AliasChoices("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY")
    )

    @field_validator("SUPABASE_URL")
    @classmethod
    def _validate_supabase_url(cls, v: str) -> str:
        stripped = v.strip().rstrip("/")
        if not stripped.startswith("https://"):
            raise ValueError("SUPABASE_URL must be an https URL")
        return stripped

    @field_validator("ADMIN_API_TOKEN")
    @classmethod
    def _validate_admin_api_token(cls, v: SecretStr) -> SecretStr:
        """Required secret — blank must fail at boot, not on first admin call.

        Stored stripped: a stray leading/trailing space in .env previously
        passed this blank check (`.strip()`) but was kept in the stored
        value, silently producing a token that could never match a real
        client's Authorization header (PR #177 review round 2).
        """
        stripped = v.get_secret_value().strip()
        if not stripped:
            raise ValueError("ADMIN_API_TOKEN must not be blank")
        return SecretStr(stripped)

    @field_validator("ADMIN_API_TOKEN_PREV")
    @classmethod
    def _validate_admin_api_token_prev(cls, v: SecretStr | None) -> SecretStr | None:
        """Optional rotation token — blank OR whitespace-only means unset, not
        malformed (matches HOLDINGS_ENCRYPTION_KEY_PREV's handling for blank;
        extended to whitespace after PR #177 review round 2 found that a
        bare space could otherwise become a live matchable credential, since
        `Authorization: Bearer  ` — two trailing spaces — parses to a
        single-space token)."""
        if v is None or not v.get_secret_value().strip():
            return None
        return SecretStr(v.get_secret_value().strip())

    # Daily Postgres -> OCI Object Storage backup (issue #106). Empty
    # namespace disables the scheduled task entirely — local dev never has
    # this set, so a locally-started Beat never uploads dev dumps anywhere.
    # Retention is enforced by the bucket's Object Lifecycle Policy, not by
    # this app — see Obsidian `Hermes/Portfonia/Portfonia Environment
    # Config.md` for the current policy (do not copy the retention number
    # here; it would drift out of sync with the actual bucket config).
    BACKUP_OCI_NAMESPACE: str = ""
    BACKUP_OCI_BUCKET: str = "portfonia-db-backups"

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
