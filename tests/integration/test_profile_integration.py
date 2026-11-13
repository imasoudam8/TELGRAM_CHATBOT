"""Live PostgreSQL integration tests for profile creation and migrations."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from src.app.config.settings import Settings, get_settings
from src.app.infrastructure.database.session import DatabaseManager
from src.app.modules.profiles.service import ProfileData, ProfileService
from src.app.modules.users.models import UserProfile
from src.app.modules.users.service import UserService
from tests.conftest import PROJECT_ROOT


@pytest.mark.asyncio
async def test_live_postgres_profile_schema_migration_and_persistence(
    disposable_postgres_db: tuple[str, Settings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute live PostgreSQL migration upgrade, profile creation, and persistence."""
    disposable_db_name, settings = disposable_postgres_db
    raw_password = settings.POSTGRES_PASSWORD.get_secret_value()

    disposable_async_url = URL.create(
        drivername="postgresql+asyncpg",
        username=settings.POSTGRES_USER,
        password=raw_password,
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        database=disposable_db_name,
    ).render_as_string(hide_password=False)

    monkeypatch.setenv("POSTGRES_DB", disposable_db_name)
    get_settings.cache_clear()

    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", disposable_async_url)

    # 1. Upgrade to previous head revision e0d35e865fec
    command.upgrade(alembic_cfg, "e0d35e865fec")

    db_manager = DatabaseManager(settings)
    await db_manager.initialize()

    try:
        # Register user on initial schema (has registered user, but no profile)
        user_service = UserService(db_manager)
        user1, _ = await user_service.register_adult_user(111001, settings.business_timezone)
        user2, _ = await user_service.register_adult_user(111002, settings.business_timezone)
        assert user1.id is not None
        assert user2.id is not None

        # 2. Run Alembic upgrade head to apply 1bd79760419b (replace_birth_date_with_age)
        command.upgrade(alembic_cfg, "head")

        # 3. Create profile via ProfileService
        profile_service = ProfileService(db_manager)
        pdata1 = ProfileData(
            display_name="کاربر شماره یک",
            age=25,
            gender="MALE",
            city="تهران",
            bio="توضیحات تست پایداری",
        )
        profile1 = await profile_service.create_profile(111001, pdata1)

        assert profile1.user_id == user1.id
        assert profile1.age == 25
        assert profile1.display_name == "کاربر شماره یک"
        assert profile1.completion_status == "COMPLETED"

        # 4. Verify unique profile ownership constraint (cannot insert duplicate profile for user1)
        with pytest.raises(IntegrityError):
            async with db_manager.session() as session:
                duplicate_profile = UserProfile(
                    user_id=user1.id,
                    age_18_plus_declared=user1.age_18_plus_declared,
                    age_declared_date=user1.age_declared_date,
                    age=30,
                    display_name="همزاد شماره یک",
                    gender="MALE",
                    completion_status="COMPLETED",
                )
                session.add(duplicate_profile)
                await session.flush()

        # 5. Verify database-level age range check constraints (age < 18 or > 99 rejected)
        test_engine = create_async_engine(disposable_async_url)
        try:
            async with test_engine.connect() as conn:
                # Under 18
                with pytest.raises(Exception) as exc_under:
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO user_profiles (user_id, age_18_plus_declared, "
                                "age_declared_date, age, display_name, gender, completion_status) "
                                f"VALUES ({user2.id}, TRUE, '{user2.age_declared_date}', 17, "
                                "'زیر سن', 'FEMALE', 'COMPLETED');"
                            )
                        )
                assert "chk_user_profiles_age_range" in str(exc_under.value).lower()

                # Over 99
                with pytest.raises(Exception) as exc_over:
                    async with conn.begin_nested():
                        await conn.execute(
                            text(
                                "INSERT INTO user_profiles (user_id, age_18_plus_declared, "
                                "age_declared_date, age, display_name, gender, completion_status) "
                                f"VALUES ({user2.id}, TRUE, '{user2.age_declared_date}', 100, "
                                "'کهنسال', 'FEMALE', 'COMPLETED');"
                            )
                        )
                assert "chk_user_profiles_age_range" in str(exc_over.value).lower()
        finally:
            await test_engine.dispose()

        # 6. Verify profile update persistence
        updated = await profile_service.update_profile_field(111001, "age", 26)
        assert updated.age == 26

        updated_name = await profile_service.update_profile_field(
            111001, "display_name", "علی جدید"
        )
        assert updated_name.display_name == "علی جدید"

        # Verify profile re-fetched from database retains updates
        refetched = await profile_service.get_profile_by_telegram_id(111001)
        assert refetched is not None
        assert refetched.age == 26
        assert refetched.display_name == "علی جدید"

    finally:
        await db_manager.close()
