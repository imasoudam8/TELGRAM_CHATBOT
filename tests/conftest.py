"""Pytest global fixtures and test isolation helpers."""

import os
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.app.config.settings import AppEnvironment, Settings
from src.app.infrastructure.database.base import Base

# Ensure tests run with clean test environment variables
os.environ["APP_ENV"] = "test"
os.environ["BOT_TOKEN"] = "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ_TEST"
os.environ["POSTGRES_PASSWORD"] = "test_postgres_password"
os.environ["TIMEZONE"] = "Asia/Tehran"


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
