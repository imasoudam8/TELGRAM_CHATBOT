"""Unit tests for Telegram runtime startup validation, polling lock, and network recovery."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.methods import GetMe, GetUpdates
from aiogram.types import Update, User

from src.app.config.settings import AppEnvironment, Settings
from src.app.infrastructure.telegram.bot import verify_telegram_bot_identity
from src.app.infrastructure.telegram.polling_lock import (
    PollingConflictError,
    TelegramPollingLock,
    extract_bot_id_from_token,
)
from tests.helpers.telegram_harness import RecordingTelegramSession


def _make_staging_settings(test_settings: Settings) -> Settings:
    return test_settings.model_copy(
        update={
            "APP_ENV": AppEnvironment.STAGING,
            "BOT_TOKEN": "111222333:ABCdefGhIJKlmNoPQRsTUVwxyZ_CI_TEST_TOKEN",
            "POSTGRES_PASSWORD": "StrongStagingDbPassword_99!",
            "REDIS_PASSWORD": "StrongStagingRedisPass_99!",
        }
    )


def test_extract_bot_id_from_token() -> None:
    """Verify bot ID prefix parsing succeeds for valid tokens and rejects malformed strings."""
    assert extract_bot_id_from_token("987654321:ABCdefGhIJKlmNoP") == "987654321"
    with pytest.raises(ValueError, match="Cannot extract"):
        extract_bot_id_from_token("not_a_numeric_id:secret")


@pytest.mark.asyncio
async def test_verify_telegram_bot_identity_staging_success_and_fast_fail_unauthorized(
    test_settings: Settings,
) -> None:
    """Verify get_me() succeeds cleanly and TelegramUnauthorizedError fails fast without retries."""
    staging = _make_staging_settings(test_settings)
    bot_user = User(id=111222333, is_bot=True, first_name="StagingBot", username="stg_test_bot")

    mock_bot = MagicMock(spec=Bot)
    mock_bot.get_me = AsyncMock(return_value=bot_user)

    result = await verify_telegram_bot_identity(mock_bot, staging)
    assert result is not None
    assert result.username == "stg_test_bot"
    mock_bot.get_me.assert_awaited_once()

    # Unauthorized token MUST fail immediately without wasting retries
    mock_bot.get_me = AsyncMock(
        side_effect=TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")
    )
    with pytest.raises(TelegramUnauthorizedError):
        await verify_telegram_bot_identity(
            mock_bot,
            staging,
            max_attempts=3,
            retry_delay_seconds=0.01,
        )
    assert mock_bot.get_me.await_count == 1


@pytest.mark.asyncio
async def test_verify_telegram_bot_identity_retries_transient_network_and_rate_limit(
    test_settings: Settings,
) -> None:
    """Verify transient network errors and bounded Retry-After recover during startup get_me()."""
    staging = _make_staging_settings(test_settings)
    bot_user = User(id=111222333, is_bot=True, first_name="StagingBot", username="stg_test_bot")

    mock_bot = MagicMock(spec=Bot)
    mock_bot.get_me = AsyncMock(
        side_effect=[
            TelegramNetworkError(method=GetMe(), message="Temporary DNS failure"),
            TelegramRetryAfter(method=GetMe(), message="Too Many Requests", retry_after=0.01),
            bot_user,
        ]
    )

    result = await verify_telegram_bot_identity(
        mock_bot,
        staging,
        max_attempts=3,
        retry_delay_seconds=0.01,
    )
    assert result == bot_user
    assert mock_bot.get_me.await_count == 3

    # Exhausting retries raises cleanly
    mock_bot.get_me = AsyncMock(
        side_effect=TelegramNetworkError(method=GetMe(), message="Persistent outage")
    )
    with pytest.raises(TelegramNetworkError):
        await verify_telegram_bot_identity(
            mock_bot,
            staging,
            max_attempts=2,
            retry_delay_seconds=0.01,
        )
    assert mock_bot.get_me.await_count == 2


@pytest.mark.asyncio
async def test_verify_telegram_bot_identity_dev_allows_offline_warning(
    test_settings: Settings,
) -> None:
    """Verify non-strict environments warn cleanly without crashing offline tests."""
    mock_bot = MagicMock(spec=Bot)
    mock_bot.get_me = AsyncMock(
        side_effect=TelegramNetworkError(method=GetMe(), message="Offline container")
    )

    result = await verify_telegram_bot_identity(mock_bot, test_settings)
    assert result is None


@pytest.mark.asyncio
async def test_polling_lock_unit_conflict_and_ownership() -> None:
    """Verify TelegramPollingLock rejects conflicts and guards renew/release by owner token."""
    fake_redis = MagicMock()
    fake_redis.set = AsyncMock(return_value=False)
    lock = TelegramPollingLock(fake_redis, bot_id="123456", ttl_seconds=30, owner_token="owner-a")

    with pytest.raises(PollingConflictError, match="Another polling instance"):
        await lock.acquire()
    assert lock.acquired is False

    fake_redis.set = AsyncMock(return_value=True)
    await lock.acquire()
    assert lock.acquired is True

    fake_redis.eval = AsyncMock(return_value=0)
    assert await lock.renew() is False
    assert lock.acquired is False

    # Already lost -> release is a safe no-op
    fake_redis.eval.reset_mock()
    assert await lock.release() is False
    fake_redis.eval.assert_not_called()


@pytest.mark.asyncio
async def test_run_app_prevents_duplicate_polling_on_conflict(test_settings: Settings) -> None:
    """Verify run_app exits non-zero cleanly when polling lock is already held."""
    from src.app.main import run_app

    mock_ctx = MagicMock()
    mock_ctx.bot = MagicMock()
    mock_ctx.dispatcher = MagicMock()
    mock_ctx.dispatcher.start_polling = AsyncMock()
    mock_ctx.redis_manager = MagicMock()
    mock_ctx.redis_manager.client = MagicMock()
    mock_ctx.redis_manager.client.set = AsyncMock(return_value=False)

    @asynccontextmanager
    async def _fake_lifespan(_settings: Settings) -> AsyncIterator[MagicMock]:
        yield mock_ctx

    with (
        patch("src.app.main.get_settings", return_value=test_settings),
        patch("src.app.main.configure_logging"),
        patch("src.app.main.application_lifespan", side_effect=_fake_lifespan),
        pytest.raises(SystemExit) as exc_info,
    ):
        await run_app()

    assert exc_info.value.code == 2
    mock_ctx.dispatcher.start_polling.assert_not_called()


@pytest.mark.asyncio
async def test_run_app_graceful_shutdown_passes_timeout_and_releases_lock(
    test_settings: Settings,
) -> None:
    """Verify run_app passes tasks_shutdown_timeout and releases polling lock on stop."""
    from src.app.main import run_app

    stopped = asyncio.Event()

    async def _fake_start_polling(*_args: object, **_kwargs: object) -> None:
        await stopped.wait()

    async def _fake_stop_polling() -> None:
        stopped.set()

    mock_ctx = MagicMock()
    mock_ctx.bot = MagicMock()
    mock_ctx.dispatcher = MagicMock()
    mock_ctx.dispatcher.resolve_used_update_types.return_value = ["message", "callback_query"]
    mock_ctx.dispatcher.start_polling = AsyncMock(side_effect=_fake_start_polling)
    mock_ctx.dispatcher.stop_polling = AsyncMock(side_effect=_fake_stop_polling)
    mock_ctx.redis_manager = MagicMock()
    mock_ctx.redis_manager.client = MagicMock()
    mock_ctx.redis_manager.client.set = AsyncMock(return_value=True)
    # Simulate lock renewal returning 0 on first tick so shutdown_event triggers cleanly
    mock_ctx.redis_manager.client.eval = AsyncMock(return_value=0)

    @asynccontextmanager
    async def _fake_lifespan(_settings: Settings) -> AsyncIterator[MagicMock]:
        yield mock_ctx

    orig_lock = TelegramPollingLock

    def _fast_lock(redis_client: object, *, bot_id: str) -> TelegramPollingLock:
        return orig_lock(
            redis_client,  # type: ignore[arg-type]
            bot_id=bot_id,
            ttl_seconds=5,
            renew_interval_seconds=0.02,
        )

    with (
        patch("src.app.main.get_settings", return_value=test_settings),
        patch("src.app.main.configure_logging"),
        patch("src.app.main.application_lifespan", side_effect=_fake_lifespan),
        patch("src.app.main.TelegramPollingLock", side_effect=_fast_lock),
    ):
        await run_app()

    mock_ctx.dispatcher.start_polling.assert_awaited_once()
    call_kwargs = mock_ctx.dispatcher.start_polling.call_args.kwargs
    assert call_kwargs["tasks_shutdown_timeout"] == 10.0
    assert call_kwargs["handle_signals"] is False
    mock_ctx.dispatcher.stop_polling.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatcher_polling_recovers_after_transient_telegram_network_error() -> None:
    """Verify aiogram Dispatcher polling retries transient TelegramNetworkError and recovers."""
    session = RecordingTelegramSession()
    bot = Bot(
        token="123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ_CI_TEST_TOKEN",
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    processed_texts: list[str] = []
    recovered_event = asyncio.Event()

    @dp.message()
    async def _on_message(message: object) -> None:
        text = getattr(message, "text", None)
        if text:
            processed_texts.append(text)
            recovered_event.set()

    update_payload = Update(
        update_id=101,
        message={
            "message_id": 1,
            "date": int(datetime.now(tz=UTC).timestamp()),
            "chat": {"id": 555001, "type": "private"},
            "from": {"id": 555001, "is_bot": False, "first_name": "Tester"},
            "text": "recovered_after_glitch",
        },
    )

    call_count = 0

    async def _fake_call(
        _self: Bot,
        method: object,
        **_kwargs: object,
    ) -> object:
        nonlocal call_count
        if isinstance(method, GetMe):
            return User(id=123456789, is_bot=True, first_name="Test", username="test_bot")
        if isinstance(method, GetUpdates):
            call_count += 1
            if call_count == 1:
                raise TelegramNetworkError(method=method, message="Temporary network drop")
            if call_count == 2:
                return [update_payload]
            await asyncio.sleep(0.05)
            return []
        return True

    try:
        from aiogram.utils.backoff import BackoffConfig

        fast_backoff = BackoffConfig(min_delay=0.01, max_delay=0.05, factor=1.2, jitter=0.0)
        with patch.object(Bot, "__call__", autospec=True, side_effect=_fake_call):
            polling_task = asyncio.create_task(
                dp.start_polling(
                    bot,
                    polling_timeout=1,
                    backoff_config=fast_backoff,
                    handle_signals=False,
                    close_bot_session=False,
                )
            )
            await asyncio.wait_for(recovered_event.wait(), timeout=3.0)
            await dp.stop_polling()
            await asyncio.wait_for(polling_task, timeout=3.0)

        assert processed_texts == ["recovered_after_glitch"]
        assert call_count >= 2
    finally:
        await bot.session.close()
