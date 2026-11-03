"""Redis client initialization, connection validation, and lifecycle management."""

import logging

from redis.asyncio import ConnectionPool, Redis
from src.app.config.settings import Settings

logger = logging.getLogger(__name__)


def create_redis_pool(settings: Settings) -> ConnectionPool:
    """Create an asynchronous Redis connection pool."""
    password = settings.REDIS_PASSWORD.get_secret_value() if settings.REDIS_PASSWORD else None
    return ConnectionPool(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        db=settings.REDIS_DB,
        password=password,
        max_connections=settings.REDIS_MAX_CONNECTIONS,
        socket_timeout=settings.REDIS_TIMEOUT,
        socket_connect_timeout=settings.REDIS_TIMEOUT,
        decode_responses=True,
    )


class RedisManager:
    """Manages Redis connection lifecycle, validation, and graceful teardown."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: ConnectionPool | None = None
        self._client: Redis | None = None

    @property
    def client(self) -> Redis:
        """Access the underlying Redis client instance."""
        if self._client is None:
            raise RuntimeError("Redis client has not been initialized. Call initialize() first.")
        return self._client

    async def initialize(self) -> None:
        """Initialize connection pool and Redis client."""
        if self._client is None:
            logger.info("Initializing Redis connection pool...")
            self._pool = create_redis_pool(self._settings)
            self._client = Redis(connection_pool=self._pool)

    async def validate_connection(self) -> bool:
        """Validate Redis connectivity by executing a PING command."""
        if self._client is None:
            await self.initialize()

        logger.debug("Validating Redis connectivity...")
        try:
            pong = await self.client.ping()
            if pong is True:
                logger.info("Redis connectivity verified successfully (PONG).")
                return True
            logger.error("Unexpected Redis ping response: %s", pong)
            return False
        except Exception as exc:
            logger.error("Redis connectivity validation failed: %s", exc)
            raise

    async def close(self) -> None:
        """Close Redis client and release all pool connections gracefully."""
        if self._client is not None:
            logger.info("Closing Redis client and connection pool...")
            await self._client.aclose()
            self._client = None
        if self._pool is not None:
            await self._pool.disconnect()
            self._pool = None
            logger.info("Redis connection pool disconnected.")


_redis_manager_instance: RedisManager | None = None


def get_redis_manager(settings: Settings | None = None) -> RedisManager:
    """Retrieve or initialize the global RedisManager singleton."""
    global _redis_manager_instance
    if _redis_manager_instance is None:
        if settings is None:
            from src.app.config.settings import get_settings

            settings = get_settings()
        _redis_manager_instance = RedisManager(settings)
    return _redis_manager_instance


def get_redis() -> Redis:
    """Dependency helper returning the initialized Redis client."""
    return get_redis_manager().client
