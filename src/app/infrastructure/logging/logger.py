"""Structured logging infrastructure with automatic sensitive data redaction."""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

from src.app.config.settings import Settings

# Regular expressions to detect and redact sensitive patterns
SENSITIVE_PATTERNS = [
    # Telegram bot token: 123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
    re.compile(r"\b\d{8,11}:[A-Za-z0-9_-]{30,50}\b"),
    # DB credentials in URIs: postgresql+asyncpg://user:pass@host
    re.compile(r"://(?P<user>[^:]+):(?P<pass>[^@]+)@"),
    # Redis URL password: redis://:pass@host
    re.compile(r"redis://:(?P<pass>[^@]+)@"),
    # Bearer tokens or secret headers
    re.compile(r"(bearer\s+)[A-Za-z0-9_\-\.]{20,}", re.IGNORECASE),
]


class SensitiveDataFilter(logging.Filter):
    """Logging filter that scrubs sensitive credentials, tokens, and connection strings."""

    def __init__(self, additional_secrets: list[str] | None = None) -> None:
        super().__init__()
        self._exact_secrets: set[str] = {
            s.strip() for s in (additional_secrets or []) if s and len(s.strip()) > 3
        }

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._redact_value(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._redact_value(v) for v in record.args)
        return True

    def redact(self, text: str) -> str:
        """Sanitize text by replacing sensitive patterns with masked placeholders."""
        if not text:
            return text

        # Exact secrets check
        for secret in self._exact_secrets:
            text = text.replace(secret, "[REDACTED]")

        # Pattern redaction
        # Telegram Bot Token
        text = SENSITIVE_PATTERNS[0].sub("[REDACTED_BOT_TOKEN]", text)
        # DB URI password
        text = SENSITIVE_PATTERNS[1].sub(r"://\g<user>:[REDACTED_DB_PASSWORD]@", text)
        # Redis URI password
        text = SENSITIVE_PATTERNS[2].sub(r"redis://:[REDACTED_REDIS_PASSWORD]@", text)
        # Bearer token
        text = SENSITIVE_PATTERNS[3].sub(r"\1[REDACTED_BEARER_TOKEN]", text)

        return text

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact(value)
        return value


class StructuredJsonFormatter(logging.Formatter):
    """JSON log formatter compliant with structured logging and observability systems."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        # Include additional attributes attached via extra={...}
        extra_keys = set(record.__dict__.keys()) - {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "message",
        }
        if extra_keys:
            log_entry["extra"] = {k: getattr(record, k) for k in extra_keys}

        return json.dumps(log_entry, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-readable console log formatter for local development."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        msg = record.getMessage()
        formatted = f"{timestamp} [{record.levelname:<8}] {record.name}: {msg}"
        if record.exc_info:
            formatted += f"\n{self.formatException(record.exc_info)}"
        return formatted


def configure_logging(settings: Settings) -> None:
    """Configure structured logging with appropriate formatters, handlers, and redaction filters."""
    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    root_logger.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)

    # Gather known secrets from settings for exact redaction
    known_secrets = [
        settings.BOT_TOKEN.get_secret_value(),
        settings.POSTGRES_PASSWORD.get_secret_value(),
    ]
    if settings.REDIS_PASSWORD:
        known_secrets.append(settings.REDIS_PASSWORD.get_secret_value())

    sensitive_filter = SensitiveDataFilter(additional_secrets=known_secrets)
    handler.addFilter(sensitive_filter)

    if settings.LOG_FORMAT.lower() == "json":
        handler.setFormatter(StructuredJsonFormatter())
    else:
        handler.setFormatter(ConsoleFormatter())

    root_logger.addHandler(handler)

    # Silence excessively noisy loggers if at INFO
    if level == logging.INFO:
        logging.getLogger("aiogram.event").setLevel(logging.WARNING)
        logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a named logger instance."""
    return logging.getLogger(name)
