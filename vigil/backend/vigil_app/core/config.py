"""Vigil settings. Prefixed env vars only — never Portfonia DB/key/queue names."""

from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

VIGIL_ROOT = Path(__file__).resolve().parents[3]


def _reject_blank(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        raise ValueError("must not be blank")
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VIGIL_",
        env_file=VIGIL_ROOT / ".env.local",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    APP_ENV: str

    DB_HOST: str
    DB_PORT: int = 5432
    DB_NAME: str
    DB_USER: str
    DB_PASSWORD: SecretStr

    REDIS_HOST: str
    REDIS_PORT: int = 6379
    REDIS_DB: int
    REDIS_KEY_PREFIX: str
    CELERY_QUEUE_PREFIX: str

    OBJECT_BUCKET: str
    OWNER_AUTH_SUBJECT: str

    KEK: SecretStr
    KEK_VERSION: str
    NOTIFICATION_KEY: SecretStr
    NONCE_KEY: SecretStr
    ALTCHA_HMAC_KEY: SecretStr

    IDENTITY_SERVICE_TOKEN: SecretStr
    OPS_API_TOKEN: SecretStr
    RESEND_API_KEY: SecretStr

    DISPATCH_ENABLED: bool = False
    RELEASE_ENABLED: bool = False
    ARMING_ENABLED: bool = False

    @field_validator(
        "APP_ENV",
        "DB_HOST",
        "DB_NAME",
        "DB_USER",
        "REDIS_HOST",
        "REDIS_KEY_PREFIX",
        "CELERY_QUEUE_PREFIX",
        "OBJECT_BUCKET",
        "OWNER_AUTH_SUBJECT",
        "KEK_VERSION",
        mode="before",
    )
    @classmethod
    def _non_blank_text(cls, value: object) -> object:
        return _reject_blank(value)

    @field_validator(
        "DB_PASSWORD",
        "KEK",
        "NOTIFICATION_KEY",
        "NONCE_KEY",
        "ALTCHA_HMAC_KEY",
        "IDENTITY_SERVICE_TOKEN",
        "OPS_API_TOKEN",
        "RESEND_API_KEY",
    )
    @classmethod
    def _non_blank_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def _distinct_keys(self) -> Self:
        if self.KEK.get_secret_value() == self.NOTIFICATION_KEY.get_secret_value():
            raise ValueError("NOTIFICATION_KEY must differ from KEK")
        return self

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.DB_USER}:{self.DB_PASSWORD.get_secret_value()}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
