"""Redis-backed distributed polling lock to prevent duplicate bot polling instances."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

DEFAULT_POLLING_LOCK_TTL_SECONDS = 30
DEFAULT_POLLING_LOCK_RENEW_INTERVAL_SECONDS = 10.0

_RENEW_LOCK_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("EXPIRE", KEYS[1], tonumber(ARGV[2]))
end
return 0
"""

_RELEASE_LOCK_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
end
return 0
"""


class PollingConflictError(RuntimeError):
    """Raised when another bot instance already holds the polling lock for this bot."""


def extract_bot_id_from_token(raw_token: str) -> str:
    """Extract numeric Telegram bot_id prefix from '<bot_id>:<secret>' without exposing token."""
    cleaned = raw_token.strip()
    bot_id, sep, _secret = cleaned.partition(":")
    if not sep or not bot_id.isdigit():
        raise ValueError("Cannot extract numeric bot_id from BOT_TOKEN.")
    return bot_id


class TelegramPollingLock:
    """Atomic Redis lease preventing multiple active polling runners for the same bot ID."""

    def __init__(
        self,
        redis_client: Redis,
        *,
        bot_id: str,
        ttl_seconds: int = DEFAULT_POLLING_LOCK_TTL_SECONDS,
        renew_interval_seconds: float = DEFAULT_POLLING_LOCK_RENEW_INTERVAL_SECONDS,
        owner_token: str | None = None,
    ) -> None:
        if ttl_seconds < 3:
            raise ValueError("ttl_seconds must be at least 3 seconds.")
        self._redis = redis_client
        self._bot_id = bot_id
        self._key = f"telegram:polling_lock:{bot_id}"
        self._ttl_seconds = ttl_seconds
        self._renew_interval_seconds = renew_interval_seconds
        self._owner_token = owner_token or uuid.uuid4().hex
        self._acquired = False
        self._renewal_task: asyncio.Task[None] | None = None

    @property
    def key(self) -> str:
        """Redis lock key scoped strictly by Telegram bot_id."""
        return self._key

    @property
    def owner_token(self) -> str:
        """Unique owner identifier for compare-and-renew / compare-and-delete checks."""
        return self._owner_token

    @property
    def acquired(self) -> bool:
        """Whether this instance currently considers the lock held."""
        return self._acquired

    async def acquire(self) -> None:
        """Acquire exclusive polling lease or raise PollingConflictError if already held."""
        ok = await self._redis.set(
            self._key,
            self._owner_token,
            nx=True,
            ex=self._ttl_seconds,
        )
        if not ok:
            raise PollingConflictError(
                f"Another polling instance is already active for Telegram bot id={self._bot_id}. "
                "Refusing to start duplicate polling."
            )
        self._acquired = True
        logger.info("Acquired distributed Telegram polling lock (bot_id=%s).", self._bot_id)

    async def renew(self) -> bool:
        """Extend lock TTL only if this instance still owns the lock."""
        if not self._acquired:
            return False
        result = await self._redis.eval(  # type: ignore[misc]
            _RENEW_LOCK_LUA,
            1,
            self._key,
            self._owner_token,
            str(self._ttl_seconds),
        )
        renewed = int(result or 0) == 1
        if not renewed:
            self._acquired = False
            logger.critical(
                "Lost distributed Telegram polling lock ownership (bot_id=%s).",
                self._bot_id,
            )
        return renewed

    async def release(self) -> bool:
        """Release the lock atomically if still owned by this instance."""
        await self.stop_renewal()
        if not self._acquired:
            return False
        try:
            result = await self._redis.eval(  # type: ignore[misc]
                _RELEASE_LOCK_LUA,
                1,
                self._key,
                self._owner_token,
            )
            released = int(result or 0) == 1
            logger.info(
                "Released distributed Telegram polling lock (bot_id=%s, removed=%s).",
                self._bot_id,
                released,
            )
            return released
        finally:
            self._acquired = False

    def start_renewal(self, on_lock_lost: Callable[[], None]) -> asyncio.Task[None]:
        """Start background TTL renewal; invokes on_lock_lost if ownership is lost."""
        if self._renewal_task is not None and not self._renewal_task.done():
            return self._renewal_task

        async def _loop() -> None:
            while self._acquired:
                await asyncio.sleep(self._renew_interval_seconds)
                if not self._acquired:
                    return
                try:
                    if not await self.renew():
                        on_lock_lost()
                        return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Failed renewing Telegram polling lock (bot_id=%s): %s",
                        self._bot_id,
                        type(exc).__name__,
                    )
                    self._acquired = False
                    on_lock_lost()
                    return

        self._renewal_task = asyncio.create_task(_loop(), name="telegram-polling-lock-renewal")
        return self._renewal_task

    async def stop_renewal(self) -> None:
        """Stop the background renewal task cleanly."""
        if self._renewal_task is not None:
            self._renewal_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._renewal_task
            self._renewal_task = None
