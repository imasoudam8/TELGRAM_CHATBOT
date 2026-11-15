"""Alembic schema migration readiness verification helpers."""

from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from src.app.infrastructure.database.session import DatabaseManager

PROJECT_ROOT = Path(__file__).resolve().parents[4]


def get_expected_alembic_heads(alembic_ini_path: Path | None = None) -> tuple[str, ...]:
    """Return expected Alembic head revision IDs configured in the repository."""
    ini_path = alembic_ini_path or (PROJECT_ROOT / "alembic.ini")
    if not ini_path.exists():
        raise FileNotFoundError(f"Alembic configuration not found at {ini_path}")

    alembic_cfg = Config(str(ini_path))
    alembic_cfg.set_main_option("script_location", str(ini_path.parent / "migrations"))
    script_dir = ScriptDirectory.from_config(alembic_cfg)
    heads = tuple(sorted(script_dir.get_heads()))
    if not heads:
        raise RuntimeError("No Alembic head revisions found in migrations directory.")
    return heads


async def get_applied_alembic_revisions(db_manager: DatabaseManager) -> tuple[str, ...]:
    """Query applied Alembic revision IDs from alembic_version in PostgreSQL."""
    async with db_manager.engine.connect() as conn:
        result = await conn.execute(text("SELECT version_num FROM alembic_version"))
        revisions = tuple(sorted(str(row[0]) for row in result.fetchall() if row[0]))
    return revisions


async def verify_database_migrations_current(
    db_manager: DatabaseManager,
    *,
    alembic_ini_path: Path | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Verify the database schema revision matches the expected Alembic head revision(s)."""
    try:
        expected_heads = get_expected_alembic_heads(alembic_ini_path=alembic_ini_path)
    except Exception as exc:
        return False, {"status": "unhealthy", "error": f"Alembic configuration error: {exc}"}

    try:
        applied = await get_applied_alembic_revisions(db_manager)
    except Exception as exc:
        return False, {
            "status": "unhealthy",
            "expected_heads": list(expected_heads),
            "error": (
                "Database migration table 'alembic_version' is missing or unreadable "
                f"({type(exc).__name__}). Run 'alembic upgrade head'."
            ),
        }

    if not applied:
        return False, {
            "status": "unhealthy",
            "expected_heads": list(expected_heads),
            "applied_revisions": [],
            "error": "Database has no applied Alembic revisions. Run 'alembic upgrade head'.",
        }

    if applied != expected_heads:
        return False, {
            "status": "unhealthy",
            "expected_heads": list(expected_heads),
            "applied_revisions": list(applied),
            "error": (
                f"Database migration mismatch: applied={list(applied)} "
                f"expected={list(expected_heads)}. Run 'alembic upgrade head'."
            ),
        }

    return True, {
        "status": "healthy",
        "expected_heads": list(expected_heads),
        "applied_revisions": list(applied),
    }


async def assert_database_migrations_current(
    db_manager: DatabaseManager,
    *,
    alembic_ini_path: Path | None = None,
) -> None:
    """Raise RuntimeError if the database schema is not migrated to Alembic head."""
    is_current, details = await verify_database_migrations_current(
        db_manager,
        alembic_ini_path=alembic_ini_path,
    )
    if not is_current:
        raise RuntimeError(str(details.get("error") or "Database migrations are not current."))
