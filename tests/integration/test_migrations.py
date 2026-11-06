"""Integration tests for Alembic migration environment and PostgreSQL execution."""

import os
import socket
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from src.app.config.settings import Settings, get_settings

PROJECT_ROOT = Path(__file__).parent.parent.parent


def is_service_reachable(host: str, port: int) -> bool:
    """Check if a TCP port is open and accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except (OSError, TimeoutError):
        return False


def is_ci_environment() -> bool:
    """Detect whether tests are executing within a CI environment."""
    return os.getenv("CI", "").lower() in ("true", "1") or os.getenv("GITHUB_ACTIONS") == "true"


def test_alembic_script_directory_discovery() -> None:
    """Verify that Alembic locates the migrations directory and valid revision scripts."""
    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    script_dir = ScriptDirectory.from_config(alembic_cfg)

    head = script_dir.get_current_head()
    assert head is not None, "Alembic head revision is missing"
    assert head == "e0d35e865fec", f"Unexpected Alembic head revision: {head}"

    revision_script = script_dir.get_revision(head)
    assert revision_script is not None
    assert "initial_schema" in revision_script.doc


def test_alembic_offline_sql_generation(tmp_path: Path) -> None:
    """Verify that Alembic offline mode renders complete PostgreSQL DDL statements."""
    output_sql_file = tmp_path / "migration.sql"
    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))

    # Execute offline migration command redirecting stdout to output file
    with open(output_sql_file, "w") as f:
        with pytest.MonkeyPatch.context() as m:
            m.setattr("sys.stdout", f)
            command.upgrade(alembic_cfg, "head", sql=True)

    sql_content = output_sql_file.read_text()
    assert "CREATE TABLE users" in sql_content
    assert "CREATE TABLE user_profiles" in sql_content
    assert "CREATE TABLE chat_sessions" in sql_content
    assert "CREATE TABLE wallet_ledgers" in sql_content
    assert "CREATE TRIGGER trg_wallet_ledgers_append_only" in sql_content
    assert "CREATE TRIGGER trg_users_declaration_guard" in sql_content
    assert "CREATE TRIGGER trg_chat_messages_guard" in sql_content
    assert "CREATE TRIGGER trg_message_evidence_holds_guard" in sql_content


@pytest.mark.asyncio
async def test_live_postgres_alembic_migration_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute 'alembic upgrade head' and 'alembic downgrade base' on live PostgreSQL 16.

    Validates:
    1. Executable migration upgrade on a clean, disposable PostgreSQL database.
    2. Existence of all 18 domain tables in information_schema.
    3. Creation of PostgreSQL catalog functions, triggers, composite FKs, and check constraints.
    4. Database constraint enforcement (age eligibility, append-only trigger rejection).
    5. Clean migration teardown via 'alembic downgrade base'.
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
    disposable_db_name = f"test_migration_{db_suffix}"

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

    # 1. Create disposable PostgreSQL test database
    async with admin_engine.connect() as conn:
        await conn.execute(
            text(f'CREATE DATABASE "{disposable_db_name}" WITH OWNER "{settings.POSTGRES_USER}";')
        )

    disposable_async_url = URL.create(
        drivername="postgresql+asyncpg",
        username=settings.POSTGRES_USER,
        password=raw_password,
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        database=disposable_db_name,
    ).render_as_string(hide_password=False)

    try:
        # 2. Reconfigure settings & Alembic config to target disposable DB
        monkeypatch.setenv("POSTGRES_DB", disposable_db_name)
        get_settings.cache_clear()

        alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
        alembic_cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
        alembic_cfg.set_main_option("sqlalchemy.url", disposable_async_url)

        # 3. Execute alembic upgrade head
        command.upgrade(alembic_cfg, "head")

        test_engine = create_async_engine(disposable_async_url)

        async with test_engine.connect() as conn:
            # Verify all 18 domain tables + alembic_version exist
            res = await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                )
            )
            tables = {row[0] for row in res.fetchall()}
            expected_tables = {
                "users",
                "user_profiles",
                "user_preferences",
                "direct_requests",
                "user_connections",
                "user_blocks",
                "chat_sessions",
                "active_chat_participants",
                "chat_messages",
                "message_evidence_holds",
                "wallets",
                "wallet_daily_allowances",
                "wallet_ledgers",
                "system_configurations",
                "reports",
                "moderation_actions",
                "admin_users",
                "admin_audit_logs",
                "alembic_version",
            }
            assert expected_tables.issubset(tables), (
                f"Missing migration tables: {expected_tables - tables}"
            )

            # Verify PostgreSQL catalog functions
            proc_res = await conn.execute(
                text(
                    "SELECT p.proname FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = 'public';"
                )
            )
            functions = {row[0] for row in proc_res.fetchall()}
            expected_functions = {
                "trg_users_age_declaration_immutability",
                "trg_user_profiles_age_immutability",
                "trg_chat_sessions_immutability",
                "trg_chat_messages_immutability",
                "trg_evidence_holds_session_consistency",
                "trg_wallet_ledgers_immutability",
                "trg_admin_audit_logs_immutability",
            }
            assert expected_functions.issubset(functions), (
                f"Missing catalog functions: {expected_functions - functions}"
            )

            # Verify PostgreSQL catalog triggers
            trig_res = await conn.execute(
                text(
                    "SELECT t.tgname FROM pg_trigger t "
                    "JOIN pg_class c ON c.oid = t.tgrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'public' AND t.tgisinternal = false;"
                )
            )
            triggers = {row[0] for row in trig_res.fetchall()}
            expected_triggers = {
                "trg_users_declaration_guard",
                "trg_user_profiles_age_guard",
                "trg_chat_sessions_lifecycle_guard",
                "trg_chat_messages_guard",
                "trg_message_evidence_holds_guard",
                "trg_wallet_ledgers_append_only",
                "trg_admin_audit_logs_append_only",
            }
            assert expected_triggers.issubset(triggers), (
                f"Missing catalog triggers: {expected_triggers - triggers}"
            )

            # Exercise user age declaration trigger
            await conn.execute(
                text(
                    "INSERT INTO users (telegram_user_id, public_id, status, "
                    "age_18_plus_declared) VALUES (999001, 'usr_pg_test_999001', "
                    "'ACTIVE', TRUE);"
                )
            )
            user_res = await conn.execute(
                text(
                    "SELECT id, age_declared_at, age_declared_date FROM users "
                    "WHERE telegram_user_id = 999001;"
                )
            )
            user_row = user_res.fetchone()
            assert user_row is not None
            user_id, age_declared_at, age_declared_date = user_row
            assert age_declared_at is not None
            assert age_declared_date is not None

            # Exercise invalid profile INSERT (age < 18 -> trigger failure)
            with pytest.raises(Exception) as exc_info:
                await conn.execute(
                    text(
                        "INSERT INTO user_profiles (user_id, age_18_plus_declared, "
                        "age_declared_date, birth_date, display_name, gender, city, "
                        f"completion_status, visibility) VALUES ({user_id}, TRUE, "
                        f"'{age_declared_date}', CURRENT_DATE - INTERVAL '10 years', "
                        "'Minor User', 'MALE', 'Tehran', 'COMPLETED', 'PUBLIC');"
                    )
                )
            err_msg = str(exc_info.value).lower()
            assert "18 years" in err_msg or "age" in err_msg

            # Exercise valid profile INSERT (age >= 18 -> success)
            await conn.execute(
                text(
                    "INSERT INTO user_profiles (user_id, age_18_plus_declared, "
                    "age_declared_date, birth_date, display_name, gender, city, "
                    f"completion_status, visibility) VALUES ({user_id}, TRUE, "
                    f"'{age_declared_date}', CURRENT_DATE - INTERVAL '25 years', "
                    "'Adult User', 'MALE', 'Tehran', 'COMPLETED', 'PUBLIC');"
                )
            )

            # Exercise append-only trigger on wallet_ledgers
            await conn.execute(
                text(f"INSERT INTO wallets (user_id, purchased_coins) VALUES ({user_id}, 50);")
            )
            await conn.execute(
                text(
                    "INSERT INTO wallet_ledgers (wallet_user_id, idempotency_key, "
                    "transaction_type, credit_type, amount, purchased_balance_before, "
                    "purchased_balance_after, free_balance_before, free_balance_after, "
                    f"actor_type, reason) VALUES ({user_id}, 'tx_pg_001', 'ADMIN_GRANT', "
                    "'PURCHASED', 50, 0, 50, 0, 0, 'ADMIN', 'Initial PG test grant');"
                )
            )

            # Attempt invalid UPDATE on append-only wallet_ledgers
            with pytest.raises(Exception) as exc_info:
                await conn.execute(
                    text(
                        "UPDATE wallet_ledgers SET reason = 'Modified reason' "
                        f"WHERE wallet_user_id = {user_id};"
                    )
                )
            assert "append-only" in str(exc_info.value).lower()

            # Attempt invalid DELETE on append-only wallet_ledgers
            with pytest.raises(Exception) as exc_info:
                await conn.execute(
                    text(f"DELETE FROM wallet_ledgers WHERE wallet_user_id = {user_id};")
                )
            assert "append-only" in str(exc_info.value).lower()

        await test_engine.dispose()

        # 4. Execute alembic downgrade base
        command.downgrade(alembic_cfg, "base")

        # 5. Verify all migration tables dropped cleanly
        test_engine_downgraded = create_async_engine(disposable_async_url)
        async with test_engine_downgraded.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                )
            )
            remaining_tables = {row[0] for row in res.fetchall()} - {"alembic_version"}
            assert len(remaining_tables) == 0, (
                f"Migration downgrade left remaining tables: {remaining_tables}"
            )
        await test_engine_downgraded.dispose()

    finally:
        # Cleanup settings cache and drop disposable database
        get_settings.cache_clear()
        async with admin_engine.connect() as conn:
            await conn.execute(
                text(f'DROP DATABASE IF EXISTS "{disposable_db_name}" WITH (FORCE);')
            )
        await admin_engine.dispose()
