"""Pytest global fixtures and test isolation helpers."""

import os
import socket
import uuid
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.app.config.settings import AppEnvironment, Settings, get_settings
from src.app.infrastructure.database.base import Base

PROJECT_ROOT = Path(__file__).parent.parent


def is_service_reachable(host: str, port: int) -> bool:
    """Check if a TCP port is open and accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except (OSError, TimeoutError):
        return False


def is_ci_environment() -> bool:
    """Detect whether tests are executing within a CI environment (e.g., GitHub Actions)."""
    return os.getenv("CI", "").lower() in ("true", "1") or os.getenv("GITHUB_ACTIONS") == "true"


# Ensure tests run with clean test environment variables without clobbering CI credentials
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("BOT_TOKEN", "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ_TEST")
os.environ.setdefault("POSTGRES_PASSWORD", "test_postgres_password")
os.environ.setdefault("TIMEZONE", "Asia/Tehran")


@pytest.fixture(autouse=True)
def reset_settings_cache() -> Generator[None, None, None]:
    """Reset the lru_cache on get_settings before and after each test."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def test_settings() -> Settings:
    """Provide isolated test settings with non-production test credentials."""
    return Settings(
        APP_ENV=AppEnvironment.TEST,
        APP_NAME="Anonymous Telegram Matching & Chat Bot [Test]",
        TIMEZONE="Asia/Tehran",
        BOT_TOKEN=SecretStr("987654321:ZYXwvUtSrQpOnMlKjIhGfEdCbA_TEST"),
        POSTGRES_HOST="localhost",
        POSTGRES_PORT=5432,
        POSTGRES_USER="test_user",
        POSTGRES_PASSWORD=SecretStr("test_secret_pass"),
        POSTGRES_DB="test_db",
        POSTGRES_ECHO=False,
        REDIS_HOST="localhost",
        REDIS_PORT=6379,
        REDIS_DB=15,
        REDIS_PASSWORD=SecretStr("test_redis_pass"),
        LOG_LEVEL="DEBUG",
        LOG_FORMAT="json",
    )


@pytest_asyncio.fixture
async def disposable_postgres_db() -> AsyncGenerator[tuple[str, Settings], None]:
    """Provide an isolated, uniquely named PostgreSQL database with guaranteed teardown.

    In CI runners, this fixture is mandatory if PostgreSQL is expected.
    In local environments without PostgreSQL containers, tests skip cleanly.
    """
    settings = Settings()
    if not is_ci_environment() and not is_service_reachable(
        settings.POSTGRES_HOST, settings.POSTGRES_PORT
    ):
        pytest.skip(
            f"PostgreSQL service not reachable on {settings.POSTGRES_HOST}:"
            f"{settings.POSTGRES_PORT} (local test environment)"
        )

    db_suffix = uuid.uuid4().hex[:8]
    disposable_db_name = f"test_db_{db_suffix}"
    raw_password = settings.POSTGRES_PASSWORD.get_secret_value()

    admin_url = URL.create(
        drivername="postgresql+asyncpg",
        username=settings.POSTGRES_USER,
        password=raw_password,
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        database=settings.POSTGRES_DB,
    ).render_as_string(hide_password=False)

    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")

    async with admin_engine.connect() as conn:
        await conn.execute(
            text(f'CREATE DATABASE "{disposable_db_name}" WITH OWNER "{settings.POSTGRES_USER}";')
        )

    disposable_settings = settings.model_copy(update={"POSTGRES_DB": disposable_db_name})

    try:
        yield disposable_db_name, disposable_settings
    finally:
        get_settings.cache_clear()
        async with admin_engine.connect() as conn:
            await conn.execute(
                text(f'DROP DATABASE IF EXISTS "{disposable_db_name}" WITH (FORCE);')
            )
        await admin_engine.dispose()


@pytest_asyncio.fixture
async def migrated_postgres_db(
    disposable_postgres_db: tuple[str, Settings],
) -> AsyncGenerator[tuple[str, Settings], None]:
    """Provide an isolated PostgreSQL database with Alembic schema migrations already applied."""
    disposable_db_name, disposable_settings = disposable_postgres_db
    raw_password = disposable_settings.POSTGRES_PASSWORD.get_secret_value()

    disposable_async_url = URL.create(
        drivername="postgresql+asyncpg",
        username=disposable_settings.POSTGRES_USER,
        password=raw_password,
        host=disposable_settings.POSTGRES_HOST,
        port=disposable_settings.POSTGRES_PORT,
        database=disposable_db_name,
    ).render_as_string(hide_password=False)

    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", disposable_async_url)
    command.upgrade(alembic_cfg, "head")

    yield disposable_db_name, disposable_settings


@pytest_asyncio.fixture
async def sqlite_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Provide an isolated in-memory SQLite async engine for database testing."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


@pytest_asyncio.fixture
async def sqlite_session(
    sqlite_engine: AsyncEngine,
) -> AsyncGenerator[AsyncSession, None]:
    """Provide an isolated async session for SQLite."""
    session_factory = async_sessionmaker(
        bind=sqlite_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session
        await session.rollback()
