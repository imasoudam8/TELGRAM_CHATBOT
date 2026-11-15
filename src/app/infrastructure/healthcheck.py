"""Health and operational readiness checker for PostgreSQL, Redis, and migrations."""

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from src.app.config.settings import Settings, get_settings
from src.app.infrastructure.database.migrations import verify_database_migrations_current
from src.app.infrastructure.database.session import DatabaseManager
from src.app.infrastructure.logging.logger import SensitiveDataFilter, configure_logging
from src.app.infrastructure.redis.client import RedisManager

logger = logging.getLogger("healthcheck")


async def run_health_checks(
    settings: Settings,
    *,
    check_migrations: bool = False,
    timeout_seconds: float = 10.0,
) -> tuple[bool, dict[str, Any]]:
    """Execute connectivity checks against PostgreSQL, Redis, and optionally Alembic head.

    Returns:
        tuple[bool, dict]: (is_healthy, details_dict)
    """
    known_secrets = [
        settings.BOT_TOKEN.get_secret_value(),
        settings.POSTGRES_PASSWORD.get_secret_value(),
    ]
    if settings.REDIS_PASSWORD:
        known_secrets.append(settings.REDIS_PASSWORD.get_secret_value())
    scrubber = SensitiveDataFilter(additional_secrets=known_secrets)

    db_manager = DatabaseManager(settings)
    redis_manager = RedisManager(settings)

    results: dict[str, Any] = {
        "status": "unhealthy",
        "database": {"status": "unknown"},
        "redis": {"status": "unknown"},
    }

    db_healthy = False
    redis_healthy = False
    migrations_healthy = True

    # Check Database (and optional Alembic head verification)
    try:
        await asyncio.wait_for(db_manager.initialize(), timeout=timeout_seconds)
        await asyncio.wait_for(db_manager.validate_connection(), timeout=timeout_seconds)
        results["database"] = {"status": "healthy", "engine": "postgresql+asyncpg"}
        db_healthy = True

        if check_migrations:
            mig_ok, mig_details = await asyncio.wait_for(
                verify_database_migrations_current(db_manager),
                timeout=timeout_seconds,
            )
            if "error" in mig_details:
                mig_details["error"] = scrubber.redact(str(mig_details["error"]))
            results["migrations"] = mig_details
            migrations_healthy = mig_ok
    except Exception as exc:
        results["database"] = {"status": "unhealthy", "error": scrubber.redact(str(exc))}
        if check_migrations:
            results["migrations"] = {"status": "unhealthy", "error": "Database unreachable"}
            migrations_healthy = False
    finally:
        try:
            await db_manager.close()
        except Exception as exc:
            logger.warning(
                "Error closing DatabaseManager during healthcheck: %s",
                scrubber.redact(str(exc)),
            )

    # Check Redis (always executed even if PostgreSQL validation or cleanup fails)
    try:
        await asyncio.wait_for(redis_manager.initialize(), timeout=timeout_seconds)
        await asyncio.wait_for(redis_manager.validate_connection(), timeout=timeout_seconds)
        results["redis"] = {"status": "healthy"}
        redis_healthy = True
    except Exception as exc:
        results["redis"] = {"status": "unhealthy", "error": scrubber.redact(str(exc))}
    finally:
        try:
            await redis_manager.close()
        except Exception as exc:
            logger.warning(
                "Error closing RedisManager during healthcheck: %s",
                scrubber.redact(str(exc)),
            )

    all_healthy = db_healthy and redis_healthy and migrations_healthy
    results["status"] = "healthy" if all_healthy else "unhealthy"

    return all_healthy, results


def main(argv: list[str] | None = None) -> None:
    """CLI entrypoint for container health and readiness check commands."""
    parser = argparse.ArgumentParser(description="Validate PostgreSQL, Redis, and schema readiness")
    parser.add_argument(
        "--check-migrations",
        action="store_true",
        help="Verify database schema revision matches Alembic head",
    )
    args, _ = parser.parse_known_args(argv)

    try:
        settings = get_settings()
        configure_logging(settings)
    except Exception as exc:
        output = {
            "status": "unhealthy",
            "error": f"Configuration failure: {exc}",
        }
        print(json.dumps(output))
        sys.exit(1)

    is_healthy, details = asyncio.run(
        run_health_checks(settings, check_migrations=args.check_migrations)
    )
    print(json.dumps(details, indent=2))
    sys.exit(0 if is_healthy else 1)


if __name__ == "__main__":
    main()
