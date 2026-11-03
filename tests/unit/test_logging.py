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
