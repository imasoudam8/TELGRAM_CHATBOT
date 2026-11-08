"""Integration tests executing against live PostgreSQL and Redis service containers."""

import os
import socket

import pytest

from src.app.config.settings import Settings
from src.app.infrastructure.database.session import DatabaseManager
from src.app.infrastructure.redis.client import RedisManager


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


@pytest.fixture
def live_service_settings() -> Settings:
    """Provide fresh settings read from environment variables without fixture overrides."""
    return Settings()


@pytest.mark.asyncio
async def test_live_postgres_connectivity(live_service_settings: Settings) -> None:
    """Verify live PostgreSQL connectivity and ping query when provisioned.

    In CI runners, this test is mandatory and will fail if the PostgreSQL service is unavailable.
    In local developer environments without live containers, this test is skipped cleanly.
    """
    if not is_ci_environment() and not is_service_reachable(
        live_service_settings.POSTGRES_HOST, live_service_settings.POSTGRES_PORT
    ):
        pytest.skip(
            f"PostgreSQL service not reachable on {live_service_settings.POSTGRES_HOST}:"
            f"{live_service_settings.POSTGRES_PORT} (local test environment)"
        )

    manager = DatabaseManager(live_service_settings)
    try:
        await manager.initialize()
        is_healthy = await manager.validate_connection()
        assert is_healthy is True
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_live_redis_connectivity(live_service_settings: Settings) -> None:
    """Verify live Redis connectivity and ping command when provisioned.

    In CI runners, this test is mandatory and will fail if the Redis service is unavailable.
    In local developer environments without live containers, this test is skipped cleanly.
    """
    if not is_ci_environment() and not is_service_reachable(
        live_service_settings.REDIS_HOST, live_service_settings.REDIS_PORT
    ):
        pytest.skip(
            f"Redis service not reachable on {live_service_settings.REDIS_HOST}:"
            f"{live_service_settings.REDIS_PORT} (local test environment)"
        )

    manager = RedisManager(live_service_settings)
    try:
        await manager.initialize()
        is_healthy = await manager.validate_connection()
        assert is_healthy is True
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_live_postgres_session_commit_and_rollback(
    live_service_settings: Settings,
) -> None:
    """Verify live PostgreSQL transaction commit and rollback behavior using DatabaseManager."""
    if not is_ci_environment() and not is_service_reachable(
        live_service_settings.POSTGRES_HOST, live_service_settings.POSTGRES_PORT
    ):
        pytest.skip(
            f"PostgreSQL service not reachable on {live_service_settings.POSTGRES_HOST}:"
            f"{live_service_settings.POSTGRES_PORT} (local test environment)"
        )

    import uuid

    from sqlalchemy import text

    manager = DatabaseManager(live_service_settings)
    await manager.initialize()

    table_name = f"test_trans_{uuid.uuid4().hex[:8]}"

    try:
        # Step 1: Create an isolated temporary test table
        async with manager.session() as session:
            await session.execute(
                text(f"CREATE TABLE {table_name} (id INT PRIMARY KEY, val TEXT NOT NULL)")
            )

        # Step 2 & 3: Insert record 1 and exit normally (auto-commit)
        async with manager.session() as session:
            await session.execute(
                text(f"INSERT INTO {table_name} (id, val) VALUES (1, 'committed_record')")
            )

        # Step 4: Verify record 1 was committed using a separate session
        async with manager.session() as session:
            res = await session.execute(text(f"SELECT val FROM {table_name} WHERE id = 1"))
            assert res.scalar_one() == "committed_record"

        # Step 5: Insert record 2 and deliberately raise an exception to trigger rollback
        with pytest.raises(RuntimeError, match="Deliberate error for rollback test"):
            async with manager.session() as session:
                await session.execute(
                    text(f"INSERT INTO {table_name} (id, val) VALUES (2, 'uncommitted_record')")
                )
                raise RuntimeError("Deliberate error for rollback test")

        # Step 6: Verify record 2 was rolled back and record 1 remains intact
        async with manager.session() as session:
            res2 = await session.execute(text(f"SELECT val FROM {table_name} WHERE id = 2"))
            assert res2.scalar_one_or_none() is None

            res1 = await session.execute(text(f"SELECT val FROM {table_name} WHERE id = 1"))
            assert res1.scalar_one() == "committed_record"

    finally:
        # Step 7: Guaranteed cleanup: drop the temporary table and close manager
        try:
            async with manager.session() as session:
                await session.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
        except Exception:
            pass
        await manager.close()


@pytest.mark.asyncio
async def test_live_redis_operations(live_service_settings: Settings) -> None:
    """Verify live Redis operations: SET with TTL, GET, positive TTL check, and DELETE."""
    if not is_ci_environment() and not is_service_reachable(
        live_service_settings.REDIS_HOST, live_service_settings.REDIS_PORT
    ):
        pytest.skip(
            f"Redis service not reachable on {live_service_settings.REDIS_HOST}:"
            f"{live_service_settings.REDIS_PORT} (local test environment)"
        )

    import uuid

    manager = RedisManager(live_service_settings)
    await manager.initialize()

    test_key = f"test:coordination:{uuid.uuid4().hex[:8]}"
    test_value = "coord_payload_verified"
    ttl_seconds = 120

    try:
        # Step 1: Set uniquely namespaced test key with TTL
        await manager.client.set(test_key, test_value, ex=ttl_seconds)

        # Step 2: Read it back and verify value
        retrieved = await manager.client.get(test_key)
        assert retrieved == test_value

        # Step 3: Verify TTL is positive and does not exceed configured expiration
        ttl = await manager.client.ttl(test_key)
        assert 0 < ttl <= ttl_seconds

        # Step 4: Delete the test key
        deleted = await manager.client.delete(test_key)
        assert deleted == 1

        # Verify key no longer exists
        assert await manager.client.get(test_key) is None
    finally:
        # Step 5: Guaranteed cleanup
        try:
            await manager.client.delete(test_key)
        except Exception:
            pass
        await manager.close()

    # Step 6: Verify Redis manager closed cleanly
    assert manager._client is None
    assert manager._pool is None


@pytest.mark.asyncio
async def test_live_postgres_user_registration(live_service_settings: Settings) -> None:
    """Verify live PostgreSQL 16 user registration and age declaration in isolated DB."""
    if not is_ci_environment() and not is_service_reachable(
        live_service_settings.POSTGRES_HOST, live_service_settings.POSTGRES_PORT
    ):
        pytest.skip(
            f"PostgreSQL service not reachable on {live_service_settings.POSTGRES_HOST}:"
            f"{live_service_settings.POSTGRES_PORT} (local test environment)"
        )

    import random
    import uuid
    from pathlib import Path
    from zoneinfo import ZoneInfo

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import select, text
    from sqlalchemy.engine import URL
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import create_async_engine

    from src.app.modules.users.models import User
    from src.app.modules.users.service import UserService

    project_root = Path(__file__).parent.parent.parent
    db_suffix = uuid.uuid4().hex[:8]
    disposable_db_name = f"test_user_reg_{db_suffix}"

    raw_password = live_service_settings.POSTGRES_PASSWORD.get_secret_value()

    admin_url = URL.create(
        drivername="postgresql+asyncpg",
        username=live_service_settings.POSTGRES_USER,
        password=raw_password,
        host=live_service_settings.POSTGRES_HOST,
        port=live_service_settings.POSTGRES_PORT,
        database=live_service_settings.POSTGRES_DB,
    ).render_as_string(hide_password=False)

    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")

    # 1. Create disposable database
    async with admin_engine.connect() as conn:
        await conn.execute(
            text(
                f'CREATE DATABASE "{disposable_db_name}" '
                f'WITH OWNER "{live_service_settings.POSTGRES_USER}";'
            )
        )

    disposable_async_url = URL.create(
        drivername="postgresql+asyncpg",
        username=live_service_settings.POSTGRES_USER,
        password=raw_password,
        host=live_service_settings.POSTGRES_HOST,
        port=live_service_settings.POSTGRES_PORT,
        database=disposable_db_name,
    ).render_as_string(hide_password=False)

    disposable_settings = live_service_settings.model_copy(
        update={"POSTGRES_DB": disposable_db_name}
    )
    manager: DatabaseManager | None = None

    try:
        # 2. Run Alembic migrations against disposable database
        alembic_cfg = Config(str(project_root / "alembic.ini"))
        alembic_cfg.set_main_option("script_location", str(project_root / "migrations"))
        alembic_cfg.set_main_option("sqlalchemy.url", disposable_async_url)
        command.upgrade(alembic_cfg, "head")

        # 3. Create DatabaseManager targeting disposable database
        manager = DatabaseManager(disposable_settings)
        await manager.initialize()

        user_service = UserService(manager)
        test_telegram_id = random.randint(1_000_000, 9_999_999_999)
        tehran_tz = ZoneInfo("Asia/Tehran")

        # Step 1: Register adult user
        user, is_new = await user_service.register_adult_user(test_telegram_id, tehran_tz)
        assert is_new is True
        assert user.telegram_user_id == test_telegram_id
        assert user.age_18_plus_declared is True
        assert user.age_declared_at is not None
        assert user.age_declared_at.tzinfo is not None
        assert user.age_declared_date is not None
        assert user.public_id.startswith("usr_")

        # Step 2: Idempotent repeated registration returns the same user without duplicate row
        user_dup, is_new_dup = await user_service.register_adult_user(test_telegram_id, tehran_tz)
        assert is_new_dup is False
        assert user_dup.id == user.id
        assert user_dup.public_id == user.public_id

        # Step 3: Verify exactly 1 user row exists in PostgreSQL for this telegram_user_id
        async with manager.session() as session:
            stmt = select(User).where(User.telegram_user_id == test_telegram_id)
            res = await session.execute(stmt)
            rows = res.scalars().all()
            assert len(rows) == 1

        # Step 4: Verify Telegram ID uniqueness constraint in PostgreSQL catalog
        with pytest.raises(SQLAlchemyError):
            async with manager.session() as session:
                async with session.begin_nested():
                    await session.execute(
                        text(
                            "INSERT INTO users (telegram_user_id, public_id, status, "
                            "age_18_plus_declared) "
                            f"VALUES ({test_telegram_id}, 'usr_dummy_collision', 'ACTIVE', TRUE)"
                        )
                    )

    finally:
        # Step 5: Clean up manager and drop disposable database
        if manager is not None:
            await manager.close()

        async with admin_engine.connect() as conn:
            await conn.execute(
                text(f'DROP DATABASE IF EXISTS "{disposable_db_name}" WITH (FORCE);')
            )
        await admin_engine.dispose()
