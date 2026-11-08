"""Centralized application configuration using Pydantic Settings."""

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):  # noqa: UP042
        """Fallback StrEnum for Python < 3.11."""

        pass


from functools import lru_cache
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class AppEnvironment(StrEnum):
    """Supported application runtime environments."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TEST = "test"


class Settings(BaseSettings):
    """Centralized typed application settings.

    Reads configuration from environment variables and an optional .env file.
    Validates types, required fields, and structural constraints.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Application identity & environment
    APP_ENV: AppEnvironment = Field(
        default=AppEnvironment.DEVELOPMENT,
        description="Application runtime environment",
    )
    APP_NAME: str = Field(
        default="Anonymous Telegram Matching & Chat Bot",
        description="Human-readable application name",
    )
    TIMEZONE: str = Field(
        default="Asia/Tehran",
        description="Business timezone identifier (e.g., Asia/Tehran)",
    )

    # Telegram Bot
    BOT_TOKEN: Annotated[
        SecretStr,
        Field(
            description="Telegram Bot API token provided by @BotFather",
        ),
    ]

    # PostgreSQL Database
    POSTGRES_HOST: str = Field(default="localhost", description="PostgreSQL host")
    POSTGRES_PORT: int = Field(default=5432, ge=1, le=65535, description="PostgreSQL port")
    POSTGRES_USER: str = Field(default="postgres", description="PostgreSQL username")
    POSTGRES_PASSWORD: SecretStr = Field(
        default=SecretStr("postgres"),
        description="PostgreSQL password",
    )
    POSTGRES_DB: str = Field(default="matching_bot", description="PostgreSQL database name")
    POSTGRES_ECHO: bool = Field(
        default=False,
        description="Echo SQL statements to standard output",
    )
    POSTGRES_POOL_SIZE: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Connection pool size",
    )
    POSTGRES_MAX_OVERFLOW: int = Field(
        default=20,
        ge=0,
        le=100,
        description="Maximum pool overflow connections",
    )
    POSTGRES_POOL_TIMEOUT: int = Field(
        default=30,
        ge=1,
        description="Pool timeout in seconds",
    )

    # Redis Cache & Coordination
    REDIS_HOST: str = Field(default="localhost", description="Redis host")
    REDIS_PORT: int = Field(default=6379, ge=1, le=65535, description="Redis port")
    REDIS_DB: int = Field(default=0, ge=0, le=15, description="Redis logical database index")
    REDIS_PASSWORD: SecretStr | None = Field(
        default=None,
        description="Redis password (optional)",
    )
    REDIS_MAX_CONNECTIONS: int = Field(
        default=20,
        ge=1,
        description="Maximum Redis connection pool size",
    )
    REDIS_TIMEOUT: float = Field(
        default=5.0,
        gt=0,
        description="Redis connection and command timeout in seconds",
    )

    # Logging
    LOG_LEVEL: str = Field(
        default="INFO",
        description="Application logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    )
    LOG_FORMAT: str = Field(
        default="json",
        description="Log output format: json or console",
    )

    @field_validator("TIMEZONE")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        """Ensure the configured timezone is valid according to the IANA database."""
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            msg = f"Invalid timezone identifier: '{value}'. Must be valid IANA name."
            raise ValueError(msg) from exc
        return value

    @field_validator("BOT_TOKEN")
    @classmethod
    def validate_bot_token(cls, value: SecretStr) -> SecretStr:
        """Validate token format: <bot_id>:<secret_hash>."""
        raw = value.get_secret_value().strip()
        if not raw:
            raise ValueError("BOT_TOKEN cannot be empty.")
        parts = raw.split(":", 1)
        if len(parts) != 2 or not parts[0].isdigit() or len(parts[1]) < 10:
            raise ValueError(
                "BOT_TOKEN must follow Telegram Bot token format: '<bot_id>:<token_secret>'."
            )
        return value

    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        """Ensure log level is standard Python log level name."""
        upper = value.upper().strip()
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if upper not in valid:
            valid_list = ", ".join(sorted(valid))
            raise ValueError(f"Invalid LOG_LEVEL '{value}'. Expected one of: {valid_list}")
        return upper

    @field_validator("LOG_FORMAT")
    @classmethod
    def validate_log_format(cls, value: str) -> str:
        """Validate log formatting option."""
        lower = value.lower().strip()
        if lower not in {"json", "console"}:
            raise ValueError(f"Invalid LOG_FORMAT '{value}'. Expected 'json' or 'console'.")
        return lower

    @model_validator(mode="after")
    def validate_production_constraints(self) -> "Settings":
        """Enforce strict security constraints when running in production."""
        if self.APP_ENV == AppEnvironment.PRODUCTION:
            raw_token = self.BOT_TOKEN.get_secret_value()
            if "EXAMPLE" in raw_token or "TEST" in raw_token:
                raise ValueError("Production environment cannot use example or test bot tokens.")

            raw_db_pass = self.POSTGRES_PASSWORD.get_secret_value()
            if raw_db_pass in {
                "postgres",
                "password",
                "root",
                "admin",
                "123456",
                "postgres_secure_dev_password",
            }:
                raise ValueError(
                    "Production environment must not use trivial or default database passwords."
                )

        return self

    @property
    def business_zoneinfo(self) -> ZoneInfo:
        """Return the parsed business timezone ZoneInfo instance."""
        return ZoneInfo(self.TIMEZONE)

    @property
    def business_timezone(self) -> ZoneInfo:
        """Alias property returning the parsed business timezone ZoneInfo instance."""
        return self.business_zoneinfo

    @property
    def async_database_url(self) -> URL:
        """Construct the asyncpg connection URL for SQLAlchemy."""
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.POSTGRES_USER,
            password=self.POSTGRES_PASSWORD.get_secret_value(),
            host=self.POSTGRES_HOST,
            port=self.POSTGRES_PORT,
            database=self.POSTGRES_DB,
        )

    @property
    def async_database_url_str(self) -> str:
        """Render database URL string with rendered password for drivers."""
        return self.async_database_url.render_as_string(hide_password=False)

    @property
    def redis_url(self) -> str:
        """Construct the Redis connection URL."""
        if self.REDIS_PASSWORD:
            pwd = self.REDIS_PASSWORD.get_secret_value()
            return f"redis://:{pwd}@{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Retrieve and cache the application settings singleton."""
    return Settings()
