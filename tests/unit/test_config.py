"""Unit tests for centralized configuration and settings validation."""

from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr, ValidationError

from src.app.config.settings import AppEnvironment, Settings


def test_valid_settings_creation(test_settings: Settings) -> None:
    """Verify that a properly configured Settings model instantiates correctly."""
    assert test_settings.APP_ENV == AppEnvironment.TEST
    assert test_settings.TIMEZONE == "Asia/Tehran"
    assert isinstance(test_settings.business_zoneinfo, ZoneInfo)
    assert test_settings.business_zoneinfo.key == "Asia/Tehran"
    assert test_settings.POSTGRES_PORT == 5432
    assert test_settings.REDIS_DB == 15


def test_bot_token_secret_masking(test_settings: Settings) -> None:
    """Ensure bot token is protected as a SecretStr and never exposed in repr."""
    token_repr = repr(test_settings.BOT_TOKEN)
    assert "**********" in token_repr
    assert "ZYXwvUtSrQpOnMlKjIhGfEdCbA" not in token_repr
    assert test_settings.BOT_TOKEN.get_secret_value().startswith("987654321:")


def test_invalid_bot_token_format() -> None:
    """Reject tokens that do not follow Telegram's bot token structure."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            BOT_TOKEN=SecretStr("invalid_token_without_colon"),
            POSTGRES_PASSWORD=SecretStr("secret"),
        )
    assert "BOT_TOKEN must follow Telegram Bot token format" in str(exc_info.value)


def test_empty_bot_token() -> None:
    """Reject empty bot tokens."""
    with pytest.raises(ValidationError):
        Settings(
            BOT_TOKEN=SecretStr(""),
            POSTGRES_PASSWORD=SecretStr("secret"),
        )


def test_invalid_timezone() -> None:
    """Reject unrecognized timezone identifiers."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            BOT_TOKEN=SecretStr("123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"),
            TIMEZONE="NonExistent/Timezone_City",
        )
    assert "Invalid timezone identifier" in str(exc_info.value)


def test_invalid_log_level() -> None:
    """Reject non-standard logging levels."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            BOT_TOKEN=SecretStr("123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"),
            LOG_LEVEL="INVALID_LEVEL",
        )
    assert "Invalid LOG_LEVEL" in str(exc_info.value)


def test_production_constraints_reject_example_token() -> None:
    """Ensure production environment prohibits example tokens."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            APP_ENV=AppEnvironment.PRODUCTION,
            BOT_TOKEN=SecretStr("123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ_EXAMPLE_TOKEN"),
            POSTGRES_PASSWORD=SecretStr("StrongProductionPassword123!"),
        )
    assert "Production environment cannot use example or test bot tokens" in str(exc_info.value)


def test_production_constraints_reject_trivial_db_password() -> None:
    """Ensure production environment prohibits default or weak DB passwords."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            APP_ENV=AppEnvironment.PRODUCTION,
            BOT_TOKEN=SecretStr("123456789:ValidProductionBotTokenHash12345"),
            POSTGRES_PASSWORD=SecretStr("postgres"),
        )
    expected_err = "Production environment must not use trivial or default database passwords"
    assert expected_err in str(exc_info.value)


def test_production_constraints_reject_known_dev_password() -> None:
    """Ensure production environment explicitly rejects known Docker Compose dev password."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            APP_ENV=AppEnvironment.PRODUCTION,
            BOT_TOKEN=SecretStr("123456789:ValidProductionBotTokenHash12345"),
            POSTGRES_PASSWORD=SecretStr("postgres_secure_dev_password"),
        )
    expected_err = "Production environment must not use trivial or default database passwords"
    assert expected_err in str(exc_info.value)

    # In development and test environments, the known dev password must remain valid
    dev_settings = Settings(
        APP_ENV=AppEnvironment.DEVELOPMENT,
        BOT_TOKEN=SecretStr("123456789:ValidDevBotTokenHash12345"),
        POSTGRES_PASSWORD=SecretStr("postgres_secure_dev_password"),
    )
    assert dev_settings.POSTGRES_PASSWORD.get_secret_value() == "postgres_secure_dev_password"

    test_settings = Settings(
        APP_ENV=AppEnvironment.TEST,
        BOT_TOKEN=SecretStr("123456789:ValidTestBotTokenHash12345"),
        POSTGRES_PASSWORD=SecretStr("postgres_secure_dev_password"),
    )
    assert test_settings.POSTGRES_PASSWORD.get_secret_value() == "postgres_secure_dev_password"


def test_docker_compose_redis_eviction_policy() -> None:
    """Verify docker-compose.yml configures Redis noeviction policy and avoids allkeys-lru."""
    from pathlib import Path

    compose_path = Path(__file__).resolve().parent.parent.parent / "docker-compose.yml"
    assert compose_path.exists(), "docker-compose.yml file not found"

    content = compose_path.read_text(encoding="utf-8")
    assert "--maxmemory-policy noeviction" in content
    assert "--maxmemory-policy allkeys-lru" not in content


def test_async_database_url_generation(test_settings: Settings) -> None:
    """Verify asynchronous database URL is constructed with asyncpg driver."""
    url = test_settings.async_database_url
    assert url.drivername == "postgresql+asyncpg"
    assert url.host == "localhost"
    assert url.port == 5432
    assert url.username == "test_user"
    assert url.database == "test_db"


def test_redis_url_generation(test_settings: Settings) -> None:
    """Verify Redis connection URL contains credentials and logical database."""
    redis_url = test_settings.redis_url
    assert redis_url == "redis://:test_redis_pass@localhost:6379/15"


def test_production_constraints_require_strong_redis_password_and_echo_false() -> None:
    """Ensure production requires strong REDIS_PASSWORD and disables POSTGRES_ECHO."""
    with pytest.raises(ValidationError, match="REDIS_PASSWORD"):
        Settings(
            APP_ENV=AppEnvironment.PRODUCTION,
            BOT_TOKEN=SecretStr("123456789:ValidProductionBotTokenHash12345"),
            POSTGRES_PASSWORD=SecretStr("StrongProductionPassword123!"),
            REDIS_PASSWORD=None,
        )

    with pytest.raises(ValidationError, match="REDIS_PASSWORD"):
        Settings(
            APP_ENV=AppEnvironment.PRODUCTION,
            BOT_TOKEN=SecretStr("123456789:ValidProductionBotTokenHash12345"),
            POSTGRES_PASSWORD=SecretStr("StrongProductionPassword123!"),
            REDIS_PASSWORD=SecretStr("short"),
        )

    with pytest.raises(ValidationError, match="POSTGRES_ECHO=false"):
        Settings(
            APP_ENV=AppEnvironment.PRODUCTION,
            BOT_TOKEN=SecretStr("123456789:ValidProductionBotTokenHash12345"),
            POSTGRES_PASSWORD=SecretStr("StrongProductionPassword123!"),
            REDIS_PASSWORD=SecretStr("StrongRedisPassword123!"),
            POSTGRES_ECHO=True,
        )

    valid_prod = Settings(
        APP_ENV=AppEnvironment.PRODUCTION,
        BOT_TOKEN=SecretStr("123456789:ValidProductionBotTokenHash12345"),
        POSTGRES_PASSWORD=SecretStr("StrongProductionPassword123!"),
        REDIS_PASSWORD=SecretStr("StrongRedisPassword123!"),
    )
    assert valid_prod.effective_require_migrations_current is True


def test_staging_constraints_reject_weak_credentials_and_require_migrations() -> None:
    """Ensure staging enforces strong credentials, non-placeholder token, and migration guard."""
    with pytest.raises(ValidationError, match="Staging environment cannot use example"):
        Settings(
            APP_ENV=AppEnvironment.STAGING,
            BOT_TOKEN=SecretStr("123456789:PLACEHOLDER_REPLACE_WITH_DEDICATED_STAGING_BOT_TOKEN"),
            POSTGRES_PASSWORD=SecretStr("StrongStagingPassword123!"),
            REDIS_PASSWORD=SecretStr("StrongStagingRedisPass123!"),
        )

    with pytest.raises(ValidationError, match="Staging environment must not use trivial"):
        Settings(
            APP_ENV=AppEnvironment.STAGING,
            BOT_TOKEN=SecretStr("123456789:ValidStagingBotTokenHash12345"),
            POSTGRES_PASSWORD=SecretStr("postgres_secure_dev_password"),
            REDIS_PASSWORD=SecretStr("StrongStagingRedisPass123!"),
        )

    with pytest.raises(ValidationError, match="REDIS_PASSWORD"):
        Settings(
            APP_ENV=AppEnvironment.STAGING,
            BOT_TOKEN=SecretStr("123456789:ValidStagingBotTokenHash12345"),
            POSTGRES_PASSWORD=SecretStr("StrongStagingPassword123!"),
            REDIS_PASSWORD=None,
        )

    with pytest.raises(ValidationError, match="POSTGRES_ECHO=false"):
        Settings(
            APP_ENV=AppEnvironment.STAGING,
            BOT_TOKEN=SecretStr("123456789:ValidStagingBotTokenHash12345"),
            POSTGRES_PASSWORD=SecretStr("StrongStagingPassword123!"),
            REDIS_PASSWORD=SecretStr("StrongStagingRedisPass123!"),
            POSTGRES_ECHO=True,
        )

    valid_staging = Settings(
        APP_ENV=AppEnvironment.STAGING,
        BOT_TOKEN=SecretStr("123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ_CI_TEST_TOKEN"),
        POSTGRES_PASSWORD=SecretStr("StrongStagingPassword123!"),
        REDIS_PASSWORD=SecretStr("StrongStagingRedisPass123!"),
    )
    assert valid_staging.effective_require_migrations_current is True
