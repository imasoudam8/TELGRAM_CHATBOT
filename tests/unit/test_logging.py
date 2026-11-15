"""Unit tests for structured logging and sensitive credential redaction."""

import json
import logging

from src.app.config.settings import Settings
from src.app.infrastructure.logging.logger import (
    ConsoleFormatter,
    SensitiveDataFilter,
    StructuredJsonFormatter,
    configure_logging,
)


def test_sensitive_data_filter_redacts_telegram_token() -> None:
    """Verify Telegram bot token pattern is sanitized."""
    filter_ = SensitiveDataFilter()
    raw_message = "Connecting with token 123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ12345 to Telegram."
    sanitized = filter_.redact(raw_message)

    assert "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ12345" not in sanitized
    assert "[REDACTED_BOT_TOKEN]" in sanitized


def test_sensitive_data_filter_redacts_database_password() -> None:
    """Verify database password in connection URL is sanitized."""
    filter_ = SensitiveDataFilter()
    raw_url = "postgresql+asyncpg://myuser:super_secret_db_pass@db.internal:5432/mydb"
    sanitized = filter_.redact(raw_url)

    assert "super_secret_db_pass" not in sanitized
    assert "postgresql+asyncpg://myuser:[REDACTED_DB_PASSWORD]@db.internal:5432/mydb" in sanitized


def test_sensitive_data_filter_redacts_redis_password() -> None:
    """Verify Redis password in connection URL is sanitized."""
    filter_ = SensitiveDataFilter()
    raw_url = "redis://:super_secret_redis_pass@cache.internal:6379/0"
    sanitized = filter_.redact(raw_url)

    assert "super_secret_redis_pass" not in sanitized
    assert "redis://:[REDACTED_REDIS_PASSWORD]@cache.internal:6379/0" in sanitized


def test_sensitive_data_filter_redacts_exact_known_secrets() -> None:
    """Verify explicitly provided known secrets are masked."""
    filter_ = SensitiveDataFilter(additional_secrets=["custom_arbitrary_secret_value"])
    raw_text = "Logging an event with custom_arbitrary_secret_value inside."
    sanitized = filter_.redact(raw_text)

    assert "custom_arbitrary_secret_value" not in sanitized
    assert "[REDACTED]" in sanitized


def test_sensitive_data_filter_exact_secret_retains_bot_token_label() -> None:
    """Verify known Telegram token in additional_secrets retains specific bot token label."""
    token = "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ_CUSTOM_TOKEN"
    arbitrary_secret = "arbitrary_external_secret_token_12345"
    filter_ = SensitiveDataFilter(additional_secrets=[token, arbitrary_secret])
    raw_text = f"Payload with token {token} and secret {arbitrary_secret}"
    sanitized = filter_.redact(raw_text)

    assert token not in sanitized
    assert arbitrary_secret not in sanitized
    assert "[REDACTED_BOT_TOKEN]" in sanitized
    assert "[REDACTED]" in sanitized


def test_structured_json_formatter_produces_valid_json() -> None:
    """Verify JSON log formatter creates schema-compliant JSON strings."""
    formatter = StructuredJsonFormatter()
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Service startup complete",
        args=(),
        exc_info=None,
    )
    formatted = formatter.format(record)
    data = json.loads(formatted)

    assert data["level"] == "INFO"
    assert data["logger"] == "test_logger"
    assert data["message"] == "Service startup complete"
    assert "timestamp" in data


def test_console_formatter_produces_readable_string() -> None:
    """Verify console log formatter creates formatted human-readable lines."""
    formatter = ConsoleFormatter()
    record = logging.LogRecord(
        name="test_logger",
        level=logging.WARNING,
        pathname="test.py",
        lineno=10,
        msg="Resource approaching limit",
        args=(),
        exc_info=None,
    )
    formatted = formatter.format(record)

    assert "[WARNING ]" in formatted
    assert "test_logger" in formatted
    assert "Resource approaching limit" in formatted


def test_configure_logging_executes_cleanly(test_settings: Settings) -> None:
    """Verify logging configuration sets up handlers without crashing."""
    configure_logging(test_settings)
    root = logging.getLogger()
    assert len(root.handlers) > 0


def test_structured_formatter_redacts_exception_and_extra_secrets() -> None:
    """Verify secrets inside exception tracebacks and extra payloads are scrubbed."""
    scrubber = SensitiveDataFilter(additional_secrets=["UltraSecretDbPass_99!"])
    formatter = StructuredJsonFormatter(scrubber=scrubber)
    try:
        raise RuntimeError(
            "Connection failed for postgresql+asyncpg://postgres:UltraSecretDbPass_99!@db:5432/app "
            "with token 987654321:ABCdefGhIJKlmNoPQRsTUVwxyZ12345"
        )
    except RuntimeError as exc:
        record = logging.LogRecord(
            name="test_logger",
            level=logging.ERROR,
            pathname="test.py",
            lineno=20,
            msg="Failure occurred",
            args=(),
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        record.dsn = "redis://:UltraSecretDbPass_99!@redis:6379/0"
        rendered = formatter.format(record)

    assert "UltraSecretDbPass_99!" not in rendered
    assert "987654321:ABCdefGhIJKlmNoPQRsTUVwxyZ12345" not in rendered
    assert "[REDACTED_DB_PASSWORD]" in rendered
    assert "[REDACTED_BOT_TOKEN]" in rendered
    assert "[REDACTED_REDIS_PASSWORD]" in rendered


def test_configure_logging_creates_bounded_rotating_file_handler(
    test_settings: Settings,
    tmp_path,
) -> None:
    """Verify LOG_FILE_PATH enables RotatingFileHandler with size/count limits."""
    from logging.handlers import RotatingFileHandler

    log_file = tmp_path / "logs" / "bot.log"
    file_settings = test_settings.model_copy(
        update={
            "LOG_FILE_PATH": str(log_file),
            "LOG_MAX_BYTES": 2048,
            "LOG_BACKUP_COUNT": 3,
        }
    )
    configure_logging(file_settings)
    try:
        root = logging.getLogger()
        rotating = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
        assert len(rotating) == 1
        assert rotating[0].maxBytes == 2048
        assert rotating[0].backupCount == 3
    finally:
        configure_logging(test_settings)
