"""Unit tests for backup retention, corruption detection, health HTTP server, and repo hardening."""

import asyncio
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app.config.settings import Settings
from src.app.infrastructure.backup import (
    assert_pg_tools_compatible,
    parse_pg_tool_major_version,
    prune_backup_retention,
    verify_backup_file,
)
from src.app.infrastructure.database.session import DatabaseManager
from src.app.infrastructure.health_server import HealthHttpServer
from src.app.infrastructure.lifecycle import application_lifespan
from src.app.infrastructure.redis.client import RedisManager

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def test_prune_backup_retention_keeps_seven_daily_and_one_monthly(tmp_path: Path) -> None:
    """Verify backup retention preserves 7 distinct daily dumps plus 1 monthly archive."""
    # Create 10 consecutive days in Nov 2026 + 1 snapshot from Oct 2026
    filenames = [f"matching_bot_202611{day:02d}T020000Z.dump.gpg" for day in range(1, 11)] + [
        "matching_bot_20261015T020000Z.dump.gpg"
    ]

    for name in filenames:
        p = tmp_path / name
        p.write_bytes(b"encrypted-payload")
        Path(f"{p}.sha256").write_text(f"abc  {name}\n", encoding="utf-8")

    newest = tmp_path / "matching_bot_20261110T020000Z.dump.gpg"
    pruned = prune_backup_retention(
        tmp_path,
        db_name="matching_bot",
        daily_keep=7,
        monthly_keep=1,
        protected_paths={newest},
    )

    remaining = sorted(p.name for p in tmp_path.glob("*.dump.gpg"))
    # Latest 7 days in Nov: Nov 04..Nov 10 (Nov 10 also serves as latest monthly for 2026-11)
    expected_remaining = [f"matching_bot_202611{day:02d}T020000Z.dump.gpg" for day in range(4, 11)]
    assert remaining == expected_remaining
    # Nov 01, 02, 03 and Oct 15 + their 4 sha256 sidecars = 8 deleted files
    assert len(pruned) == 8
    assert not (tmp_path / "matching_bot_20261015T020000Z.dump.gpg.sha256").exists()


def test_verify_backup_file_rejects_missing_empty_corrupt_and_bad_checksum(tmp_path: Path) -> None:
    """Verify empty, missing, mismatched-checksum, and garbage backups fail loudly."""
    missing = tmp_path / "does_not_exist.dump"
    with pytest.raises(FileNotFoundError):
        verify_backup_file(missing)

    empty_file = tmp_path / "empty.dump"
    empty_file.write_bytes(b"")
    with pytest.raises(ValueError, match="empty"):
        verify_backup_file(empty_file)

    corrupt_file = tmp_path / "corrupt.dump"
    corrupt_file.write_bytes(b"not a valid postgres archive")
    checksum_file = tmp_path / "corrupt.dump.sha256"
    checksum_file.write_text("0000deadbeef  corrupt.dump\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Checksum mismatch"):
        verify_backup_file(corrupt_file, require_checksum=True)

    checksum_file.unlink()
    with pytest.raises(RuntimeError, match="Command failed"):
        verify_backup_file(corrupt_file)


@pytest.mark.asyncio
async def test_health_http_server_liveness_and_readiness(test_settings: Settings) -> None:
    """Verify internal HealthHttpServer serves /health (200) and /ready (200/503)."""
    ephemeral_settings = test_settings.model_copy(
        update={"HEALTH_SERVER_ENABLED": True, "HEALTH_HOST": "127.0.0.1", "HEALTH_PORT": 0}
    )
    server = HealthHttpServer(ephemeral_settings)
    await server.start()
    port = server.bound_port
    assert port is not None and port > 0

    async def _http_get(path: str) -> tuple[int, dict]:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode("ascii"))
        await writer.drain()
        raw = await reader.read()
        writer.close()
        await writer.wait_closed()
        head, body = raw.split(b"\r\n\r\n", 1)
        status_code = int(head.decode("ascii").split(" ", 2)[1])
        return status_code, json.loads(body.decode("utf-8"))

    try:
        code, payload = await _http_get("/health")
        assert code == 200
        assert payload["status"] == "alive"

        with patch(
            "src.app.infrastructure.health_server.run_health_checks",
            new=AsyncMock(
                return_value=(
                    True,
                    {
                        "status": "healthy",
                        "database": {"status": "healthy"},
                        "redis": {"status": "healthy"},
                    },
                )
            ),
        ):
            code_ok, payload_ok = await _http_get("/ready")
            assert code_ok == 200
            assert payload_ok["status"] == "healthy"

        with patch(
            "src.app.infrastructure.health_server.run_health_checks",
            new=AsyncMock(
                return_value=(
                    False,
                    {
                        "status": "unhealthy",
                        "database": {"status": "unhealthy"},
                        "redis": {"status": "healthy"},
                    },
                )
            ),
        ):
            code_fail, payload_fail = await _http_get("/ready")
            assert code_fail == 503
            assert payload_fail["status"] == "unhealthy"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_lifecycle_fails_fast_when_migrations_required_and_behind(
    test_settings: Settings,
) -> None:
    """Verify application_lifespan aborts startup if REQUIRE_MIGRATIONS_CURRENT=True fails."""
    strict_settings = test_settings.model_copy(update={"REQUIRE_MIGRATIONS_CURRENT": True})
    mock_db = MagicMock(spec=DatabaseManager)
    mock_db.initialize = AsyncMock()
    mock_db.validate_connection = AsyncMock(return_value=True)
    mock_db.close = AsyncMock()

    mock_redis = MagicMock(spec=RedisManager)
    mock_redis.initialize = AsyncMock()
    mock_redis.close = AsyncMock()

    with (
        patch(
            "src.app.infrastructure.lifecycle.assert_database_migrations_current",
            new=AsyncMock(side_effect=RuntimeError("Database migration mismatch")),
        ),
        pytest.raises(RuntimeError, match="Database migration mismatch"),
    ):
        async with application_lifespan(
            strict_settings,
            db_manager=mock_db,
            redis_manager=mock_redis,
        ):
            pytest.fail("Should not start bot when required migrations are not current")

    mock_db.close.assert_awaited_once()
    mock_redis.initialize.assert_not_called()


def test_docker_compose_ci_and_secret_hygiene() -> None:
    """Validate Docker/Compose security invariants, safe deploy workflow, and secret hygiene."""
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER appuser" in dockerfile
    assert "STOPSIGNAL SIGTERM" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "postgresql-client-16" in dockerfile
    assert "gnupg" in dockerfile

    ci_yml = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "postgresql-client-16" in ci_yml
    assert "Verify PostgreSQL 16 Client Tool Compatibility" in ci_yml

    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert ".env" in dockerignore
    assert ".git/" in dockerignore

    compose_dev = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "ports:" not in compose_dev
    assert "max-size:" in compose_dev
    assert "max-file:" in compose_dev
    assert "no-new-privileges:true" in compose_dev
    assert "--maxmemory-policy noeviction" in compose_dev

    compose_prod = (PROJECT_ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    assert "APP_ENV: production" in compose_prod
    assert "${BOT_TOKEN:?" in compose_prod
    assert "${POSTGRES_PASSWORD:?" in compose_prod
    assert "${REDIS_PASSWORD:?" in compose_prod
    assert "read_only: true" in compose_prod
    assert "--check-migrations" in compose_prod

    deploy_yml = (PROJECT_ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in deploy_yml
    assert "alembic downgrade" not in deploy_yml

    # Ensure zero real Telegram bot tokens or hardcoded private keys are checked into tracked source
    real_token_re = re.compile(r"\b\d{8,11}:[A-Za-z0-9_-]{30,50}\b")
    for rel_dir in ("src", "scripts", ".github"):
        for file_path in (PROJECT_ROOT / rel_dir).rglob("*"):
            if not file_path.is_file() or file_path.suffix in {".pyc", ".png"}:
                continue
            text = file_path.read_text(encoding="utf-8", errors="ignore")
            for match in real_token_re.findall(text):
                assert any(marker in match.upper() for marker in ("EXAMPLE", "TEST", "CI_TEST")), (
                    f"Suspicious non-test bot token literal found in {file_path}"
                )


def test_pg_client_version_parsing_and_compatibility_guard(test_settings: Settings) -> None:
    """Verify pg_dump/pg_restore major version parser and client > server rejection."""
    assert parse_pg_tool_major_version("pg_dump (PostgreSQL) 16.16 (Debian 16.16-1.pgdg12+1)") == 16
    assert (
        parse_pg_tool_major_version("pg_restore (PostgreSQL) 17.2 (Ubuntu 17.2-1.pgdg24.04+1)")
        == 17
    )
    with pytest.raises(ValueError, match="Could not parse"):
        parse_pg_tool_major_version("unknown binary output")

    with (
        patch("src.app.infrastructure.backup.get_pg_server_major_version", return_value=16),
        patch(
            "src.app.infrastructure.backup.get_pg_tool_major_version",
            side_effect=lambda tool: 17 if tool == "pg_dump" else 16,
        ),
        pytest.raises(RuntimeError, match="transaction_timeout"),
    ):
        assert_pg_tools_compatible(test_settings, tools=("pg_dump", "pg_restore"))

    with (
        patch("src.app.infrastructure.backup.get_pg_server_major_version", return_value=16),
        patch("src.app.infrastructure.backup.get_pg_tool_major_version", return_value=16),
    ):
        versions = assert_pg_tools_compatible(test_settings, tools=("pg_dump", "pg_restore"))
        assert versions == {"server": 16, "pg_dump": 16, "pg_restore": 16}
