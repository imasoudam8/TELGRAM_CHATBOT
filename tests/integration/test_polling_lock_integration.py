"""Live Redis integration tests for distributed Telegram polling lock guarantees."""

from __future__ import annotations

import pytest

from src.app.config.settings import Settings
from src.app.infrastructure.redis.client import RedisManager
from src.app.infrastructure.telegram.polling_lock import (
    PollingConflictError,
    TelegramPollingLock,
)


@pytest.mark.asyncio
async def test_live_redis_polling_lock_prevents_duplicate_instances_and_supports_clean_restart(
    test_settings: Settings,
) -> None:
    """Verify Redis polling lock blocks duplicate runners for same bot_id and releases cleanly."""
    redis_manager = RedisManager(test_settings)
    await redis_manager.initialize()
    try:
        await redis_manager.validate_connection()
        client = redis_manager.client

        # Ensure clean slate for test bot IDs
        await client.delete("telegram:polling_lock:888001", "telegram:polling_lock:888002")

        runner_a = TelegramPollingLock(
            client,
            bot_id="888001",
            ttl_seconds=15,
            owner_token="instance-a",
        )
        runner_b = TelegramPollingLock(
            client,
            bot_id="888001",
            ttl_seconds=15,
            owner_token="instance-b",
        )
        other_bot = TelegramPollingLock(
            client,
            bot_id="888002",
            ttl_seconds=15,
            owner_token="staging-other-bot",
        )

        # 1. First instance acquires polling lock cleanly
        await runner_a.acquire()
        assert await client.get("telegram:polling_lock:888001") == "instance-a"

        # 2. Second concurrent instance for the SAME bot_id MUST be rejected immediately
        with pytest.raises(PollingConflictError, match="888001"):
            await runner_b.acquire()

        # 3. Different bot_id (e.g. isolated staging vs prod bot token) is unaffected
        await other_bot.acquire()
        assert other_bot.acquired is True

        # 4. Lock renewal extends TTL for valid owner only
        assert await runner_a.renew() is True
        ttl = await client.ttl("telegram:polling_lock:888001")
        assert 1 <= ttl <= 15

        # 5. Graceful shutdown release removes lock and permits immediate restart re-acquisition
        assert await runner_a.release() is True
        assert await client.get("telegram:polling_lock:888001") is None

        await runner_b.acquire()
        assert runner_b.acquired is True

        await runner_b.release()
        await other_bot.release()
    finally:
        await redis_manager.close()
