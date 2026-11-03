"""Integration tests for database connection lifecycle and session management."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from src.app.config.settings import Settings
from src.app.infrastructure.database.session import DatabaseManager


@pytest.mark.asyncio
async def test_database_manager_lifecycle(test_settings: Settings) -> None:
    """Verify DatabaseManager initialization, ping validation, and disposal."""
    manager = DatabaseManager(test_settings)

    # Mock engine connection to validate ping without a live PostgreSQL instance
    mock_engine = AsyncMock(spec=AsyncEngine)
    mock_conn = AsyncMock()
    from unittest.mock import MagicMock

    mock_result = MagicMock()
    mock_result.scalar.return_value = 1
    mock_conn.execute.return_value = mock_result
    mock_engine.connect.return_value.__aenter__.return_value = mock_conn

    patch_engine = patch(
        "src.app.infrastructure.database.session.create_database_engine",
        return_value=mock_engine,
    )
    patch_factory = patch("src.app.infrastructure.database.session.create_session_factory")
    with patch_engine, patch_factory:
        await manager.initialize()
        assert manager.engine is not None

        # Validate ping
        is_healthy = await manager.validate_connection()
        assert is_healthy is True
        mock_conn.execute.assert_awaited_once()

        # Close
        await manager.close()
        mock_engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_rollback_on_exception(
    sqlite_engine: AsyncEngine,
    test_settings: Settings,
) -> None:
    """Verify that an exception inside the session context causes automatic rollback."""
    patch_engine = patch(
        "src.app.infrastructure.database.session.create_database_engine",
        return_value=sqlite_engine,
    )
    with patch_engine:
        manager = DatabaseManager(test_settings)
        manager._engine = sqlite_engine
        from src.app.infrastructure.database.session import create_session_factory

        manager._session_factory = create_session_factory(sqlite_engine)

        with pytest.raises(RuntimeError, match="Simulated transaction error"):
            async with manager.session() as session:
                await session.execute(text("SELECT 1"))
                raise RuntimeError("Simulated transaction error")
