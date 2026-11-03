"""Async SQLAlchemy engine, session factory, and lifecycle manager."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.app.config.settings import Settings

logger = logging.getLogger(__name__)


def create_database_engine(settings: Settings) -> AsyncEngine:
    """Create a configured AsyncEngine instance using asyncpg."""
    return create_async_engine(
        settings.async_database_url,
        echo=settings.POSTGRES_ECHO,
        pool_size=settings.POSTGRES_POOL_SIZE,
        max_overflow=settings.POSTGRES_MAX_OVERFLOW,
        pool_timeout=settings.POSTGRES_POOL_TIMEOUT,
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an async session factory bound to the engine."""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )


class DatabaseManager:
    """Encapsulates database lifecycle: initialization, validation, and graceful teardown."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def engine(self) -> AsyncEngine:
        """Access the underlying engine, raising an error if uninitialized."""
        if self._engine is None:
            raise RuntimeError("Database engine has not been initialized. Call initialize() first.")
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Access the session factory, raising an error if uninitialized."""
        if self._session_factory is None:
            raise RuntimeError(
                "Database session factory has not been initialized. Call initialize() first."
            )
        return self._session_factory

    async def initialize(self) -> None:
        """Initialize the database engine and session factory."""
        if self._engine is None:
            logger.info("Initializing PostgreSQL database engine...")
            self._engine = create_database_engine(self._settings)
            self._session_factory = create_session_factory(self._engine)

    async def validate_connection(self) -> bool:
        """Validate database connectivity by executing a lightweight ping query ('SELECT 1')."""
        if self._engine is None:
            await self.initialize()

        logger.debug("Validating PostgreSQL connectivity...")
        try:
            async with self.engine.connect() as conn:
                result = await conn.execute(text("SELECT 1"))
                scalar = result.scalar()
                if scalar == 1:
                    logger.info("PostgreSQL connectivity verified successfully.")
                    return True
                logger.error("Unexpected response from database ping: %s", scalar)
                return False
        except Exception as exc:
            logger.error("PostgreSQL connectivity validation failed: %s", exc)
            raise

    async def close(self) -> None:
        """Dispose of the connection pool gracefully."""
        if self._engine is not None:
            logger.info("Disposing PostgreSQL connection pool...")
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
            logger.info("PostgreSQL connection pool disposed.")

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """Provide a transactional async database session context."""
        if self._session_factory is None:
            await self.initialize()

        assert self._session_factory is not None
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise


_db_manager_instance: DatabaseManager | None = None


def get_database_manager(settings: Settings | None = None) -> DatabaseManager:
    """Retrieve or initialize the global DatabaseManager singleton."""
    global _db_manager_instance
    if _db_manager_instance is None:
        if settings is None:
            from src.app.config.settings import get_settings

            settings = get_settings()
        _db_manager_instance = DatabaseManager(settings)
    return _db_manager_instance


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency helper yielding an async session."""
    manager = get_database_manager()
    async with manager.session() as session:
        yield session
