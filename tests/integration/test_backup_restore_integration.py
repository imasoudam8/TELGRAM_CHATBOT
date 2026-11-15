"""Live PostgreSQL 16 + Redis 7 tests for migration readiness and backup/restore drills."""

import os
import shutil
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from src.app.config.settings import Settings
from src.app.infrastructure.backup import (
    SUPPORTED_POSTGRES_MAJOR,
    assert_pg_tools_compatible,
    create_postgres_backup,
    get_pg_tool_major_version,
    restore_and_verify_backup,
    verify_backup_file,
)
from src.app.infrastructure.database.migrations import verify_database_migrations_current
from src.app.infrastructure.database.session import DatabaseManager
from src.app.infrastructure.healthcheck import run_health_checks
from src.app.modules.profiles.service import ProfileData, ProfileService
from src.app.modules.users.service import UserService
from tests.conftest import PROJECT_ROOT, is_ci_environment, is_service_reachable


@pytest.mark.asyncio
async def test_live_postgres_existing_upgrade_and_migration_readiness(
    disposable_postgres_db: tuple[str, Settings],
) -> None:
    """Verify migration readiness checks on empty DB, older revision upgrade, and healthcheck."""
    db_name, settings = disposable_postgres_db
    raw_password = settings.POSTGRES_PASSWORD.get_secret_value()
    db_url = URL.create(
        drivername="postgresql+asyncpg",
        username=settings.POSTGRES_USER,
        password=raw_password,
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        database=db_name,
    ).render_as_string(hide_password=False)

    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    db = DatabaseManager(settings)
    await db.initialize()
    try:
        # 1. Fresh unmigrated database must fail migration readiness
        ok_empty, details_empty = await verify_database_migrations_current(db)
        assert ok_empty is False
        assert details_empty["status"] == "unhealthy"

        # 2. Upgrade only to initial schema revision (simulating pre-existing deployment)
        command.upgrade(alembic_cfg, "e0d35e865fec")
        ok_behind, details_behind = await verify_database_migrations_current(db)
        assert ok_behind is False
        assert "mismatch" in details_behind["error"].lower()

        # 3. Seed existing user row prior to upgrading to head (verifies upgrade preserves data)
        async with db.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (telegram_user_id, public_id, status, "
                    "age_18_plus_declared) VALUES (880001, 'usr_upgrade_test_01', 'ACTIVE', TRUE);"
                )
            )

        # 4. Upgrade existing DB to head
        command.upgrade(alembic_cfg, "head")
        ok_head, details_head = await verify_database_migrations_current(db)
        assert ok_head is True
        assert details_head["applied_revisions"] == ["1bd79760419b"]

        async with db.engine.connect() as conn:
            cnt = (
                await conn.execute(
                    text("SELECT count(*) FROM users WHERE public_id = 'usr_upgrade_test_01';")
                )
            ).scalar_one()
            assert cnt == 1

        # 5. Live healthcheck with --check-migrations equivalent against PostgreSQL 16 + Redis 7
        if is_ci_environment() or is_service_reachable(settings.REDIS_HOST, settings.REDIS_PORT):
            is_healthy, health_details = await run_health_checks(settings, check_migrations=True)
            assert is_healthy is True
            assert health_details["status"] == "healthy"
            assert health_details["migrations"]["status"] == "healthy"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_live_postgres_backup_gpg_corruption_detection_and_restore_drill(
    migrated_postgres_db: tuple[str, Settings],
    tmp_path: Path,
) -> None:
    """Verify pg_dump, GPG encryption, corruption detection, and restore into disposable DB."""
    if shutil.which("pg_dump") is None or shutil.which("pg_restore") is None:
        pytest.fail("pg_dump and pg_restore binaries are required for backup/restore integration")
    if shutil.which("gpg") is None:
        pytest.fail("gpg binary is required for encrypted backup integration")

    _, settings = migrated_postgres_db

    assert get_pg_tool_major_version("pg_dump") == SUPPORTED_POSTGRES_MAJOR
    assert get_pg_tool_major_version("pg_restore") == SUPPORTED_POSTGRES_MAJOR
    assert get_pg_tool_major_version("psql") == SUPPORTED_POSTGRES_MAJOR
    versions = assert_pg_tools_compatible(settings, tools=("pg_dump", "pg_restore"))
    assert versions == {"server": 16, "pg_dump": 16, "pg_restore": 16}

    db = DatabaseManager(settings)
    await db.initialize()
    try:
        user, created = await UserService(db).register_adult_user(
            890001, settings.business_timezone
        )
        assert created is True
        await ProfileService(db).create_profile(
            890001,
            ProfileData(
                display_name="آرش",
                age=27,
                gender="MALE",
                city="تهران",
                visibility="PUBLIC",
            ),
        )
    finally:
        await db.close()

    backup_dir = tmp_path / "pg_backups"
    passphrase = "DrillBackupEncryptionPass_2026!"

    # Requiring remote when unconfigured must fail explicitly rather than faking success
    with pytest.raises(RuntimeError, match="Off-server backup destination is required"):
        create_postgres_backup(
            settings,
            backup_dir=backup_dir,
            gpg_passphrase=passphrase,
            remote_target=None,
            require_remote=True,
        )

    backup_res = create_postgres_backup(
        settings,
        backup_dir=backup_dir,
        gpg_passphrase=passphrase,
        remote_target=None,
        require_remote=False,
    )
    assert backup_res.encrypted is True
    assert backup_res.size_bytes > 0
    assert backup_res.toc_entries >= 18
    assert backup_res.offsite_status == "skipped_unconfigured"
    assert backup_res.backup_path.suffix == ".gpg"
    # Verify unencrypted raw .dump was removed after encryption
    assert list(backup_dir.glob("*.dump")) == []
    assert (os.stat(backup_dir).st_mode & 0o777) == 0o700
    assert (os.stat(backup_res.backup_path).st_mode & 0o777) == 0o600

    # Wrong passphrase must fail verification
    with pytest.raises(RuntimeError):
        verify_backup_file(
            backup_res.backup_path,
            require_checksum=True,
            passphrase="WrongPassphrase!",
            settings=settings,
        )

    # Truncated/corrupted backup must fail verification
    corrupt_copy = tmp_path / "truncated.dump.gpg"
    corrupt_copy.write_bytes(backup_res.backup_path.read_bytes()[:64])
    with pytest.raises(RuntimeError):
        verify_backup_file(corrupt_copy, passphrase=passphrase, settings=settings)

    # Restore encrypted backup into a brand-new disposable PostgreSQL 16 database
    restored_db_name = f"restore_drill_{uuid.uuid4().hex[:8]}"
    admin_url = URL.create(
        drivername="postgresql+asyncpg",
        username=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD.get_secret_value(),
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        database="postgres",
    ).render_as_string(hide_password=False)

    try:
        restore_res = restore_and_verify_backup(
            settings,
            backup_path=backup_res.backup_path,
            target_db=restored_db_name,
            create_target_db=True,
            passphrase=passphrase,
        )
        assert restore_res.target_db == restored_db_name
        assert restore_res.alembic_revisions == ("1bd79760419b",)
        assert restore_res.tables_count >= 18
        assert restore_res.triggers_count >= 7
        assert restore_res.users_count == 1
        assert user.public_id is not None
    finally:
        admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with admin_engine.connect() as conn:
                await conn.execute(
                    text(f'DROP DATABASE IF EXISTS "{restored_db_name}" WITH (FORCE);')
                )
        finally:
            await admin_engine.dispose()
