"""Health and operational readiness checker for PostgreSQL, Redis, and application status."""

import asyncio
import json
import logging
import sys
from typing import Any

from src.app.config.settings import Settings, get_settings
from src.app.infrastructure.database.session import DatabaseManager
from src.app.infrastructure.logging.logger import configure_logging
from src.app.infrastructure.redis.client import RedisManager

logger = logging.getLogger("healthcheck")


async def run_health_checks(settings: Settings) -> tuple[bool, dict[str, Any]]:
    """Execute connectivity checks against PostgreSQL and Redis.

    Returns:
        tuple[bool, dict]: (is_healthy, details_dict)
    """
    db_manager = DatabaseManager(settings)
    redis_manager = RedisManager(settings)

    results: dict[str, Any] = {
        "status": "unhealthy",
        "database": {"status": "unknown"},
        "redis": {"status": "unknown"},
    }

    db_healthy = False
    redis_healthy = False

    # Check Database
    try:
        await db_manager.initialize()
        await db_manager.validate_connection()
        results["database"] = {"status": "healthy", "engine": "postgresql+asyncpg"}
        db_healthy = True
    except Exception as exc:
        results["database"] = {"status": "unhealthy", "error": str(exc)}
    finally:
        await db_manager.close()

    # Check Redis
    try:
        await redis_manager.initialize()
        await redis_manager.validate_connection()
        results["redis"] = {"status": "healthy"}
        redis_healthy = True
    except Exception as exc:
        results["redis"] = {"status": "unhealthy", "error": str(exc)}
    finally:
        await redis_manager.close()

    all_healthy = db_healthy and redis_healthy
    results["status"] = "healthy" if all_healthy else "unhealthy"

    return all_healthy, results


def main() -> None:
    """CLI entrypoint for container health check commands."""
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

    is_healthy, details = asyncio.run(run_health_checks(settings))
    print(json.dumps(details, indent=2))
    sys.exit(0 if is_healthy else 1)


if __name__ == "__main__":
    main()
