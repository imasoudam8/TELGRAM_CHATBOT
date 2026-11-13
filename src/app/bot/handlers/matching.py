"""Telegram handlers for random matchmaking queue entry, cancellation, and pairing."""

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.app.bot.handlers.common import (
    MSG_GENERIC_ERROR,
    load_authorized_user,
    notify_chat_started,
    reply_with_auth_error,
    resolve_redis_manager,
)
from src.app.bot.keyboards.chat import (
    CB_MATCH_JOIN,
    CB_MATCH_LEAVE,
    get_active_chat_reply_keyboard,
    get_matching_entry_keyboard,
    get_matching_waiting_keyboard,
)
from src.app.bot.keyboards.onboarding import BTN_RANDOM_CHAT, get_main_menu_keyboard
from src.app.config.settings import Settings, get_settings
from src.app.infrastructure.database.session import DatabaseManager, get_database_manager
from src.app.infrastructure.redis.client import RedisManager
from src.app.modules.matching.service import RandomMatchService

logger = logging.getLogger(__name__)


async def _handle_join_matching_flow(
    target: Message,
    telegram_user_id: int,
    db_manager: DatabaseManager,
    redis_manager: RedisManager | None,
    settings: Settings | None = None,
) -> None:
    user, err = await load_authorized_user(
        db_manager,
        telegram_user_id,
        require_completed_profile=True,
        allow_chat_restricted=False,
        settings=settings,
    )
    if err is not None or user is None:
        await reply_with_auth_error(target, err or MSG_GENERIC_ERROR)
        return

    match_service = RandomMatchService(db_manager, resolve_redis_manager(redis_manager))
    result = await match_service.enter_queue_and_find_match(user.id)

    if result.status == "ALREADY_IN_CHAT":
        await target.answer(
            "💬 شما هم‌اکنون در یک گفتگوی فعال هستید. ابتدا گفتگوی فعلی خود را پایان دهید.",
            reply_markup=get_active_chat_reply_keyboard(),
        )
        return

    if result.status == "MATCHED":
        assert (
            result.chat_session_id is not None
            and result.seeker_telegram_id is not None
            and result.partner_telegram_id is not None
            and result.seeker_profile is not None
            and result.partner_profile is not None
        )
        intro = (
            "🎲 <b>هم‌صحبت تصادفی یافت شد و گفتگو آغاز گردید!</b>"
            if not result.matched_via_relaxed
            else "🎲 <b>هم‌صحبت تصادفی با فیلتر قابل انعطاف یافت شد!</b>"
        )
        await notify_chat_started(
            target.bot,
            chat_id=result.chat_session_id,
            user1_telegram_id=result.seeker_telegram_id,
            user1_sees_profile=result.partner_profile,
            user2_telegram_id=result.partner_telegram_id,
            user2_sees_profile=result.seeker_profile,
            intro_title=intro,
        )
        return

    await target.answer(
        "🎲 <b>در صف جستجوی گفتگوی تصادفی قرار گرفتید...</b>\n\n"
        "به محض یافتن هم‌صحبت متناسب با فیلترهای شما، گفتگو به صورت خودکار آغاز خواهد شد.",
        reply_markup=get_matching_waiting_keyboard(),
    )


async def handle_random_chat_menu_entry(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    redis_manager: RedisManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Handle '🎲 گفتگوی تصادفی' main menu button."""
    if message.from_user is None:
        return

    await state.clear()
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        await _handle_join_matching_flow(
            message,
            message.from_user.id,
            resolved_db,
            redis_manager,
            settings=resolved_settings,
        )
    except PermissionError as perm_err:
        await message.answer(str(perm_err), reply_markup=get_main_menu_keyboard())
    except Exception as exc:
        logger.error("Error entering random matching queue: %s", type(exc).__name__)
        await message.answer(MSG_GENERIC_ERROR)


async def handle_match_join_callback(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    redis_manager: RedisManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Handle retry/join random matching inline button."""
    await callback.answer()
    if callback.from_user is None or not isinstance(callback.message, Message):
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        await _handle_join_matching_flow(
            callback.message,
            callback.from_user.id,
            resolved_db,
            redis_manager,
            settings=resolved_settings,
        )
    except PermissionError as perm_err:
        await callback.message.answer(str(perm_err), reply_markup=get_main_menu_keyboard())
    except Exception as exc:
        logger.error("Error joining random matching queue: %s", type(exc).__name__)
        await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_match_leave_callback(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    redis_manager: RedisManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Handle leaving random matching queue."""
    await callback.answer("جستجو متوقف شد.")
    if callback.from_user is None or not isinstance(callback.message, Message):
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, _ = await load_authorized_user(resolved_db, callback.from_user.id)
        if user is not None:
            match_service = RandomMatchService(resolved_db, resolve_redis_manager(redis_manager))
            await match_service.leave_queue(user.id)

        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer(
            "⏹ جستجوی تصادفی لغو گردید.",
            reply_markup=get_matching_entry_keyboard(),
        )
    except Exception as exc:
        logger.error("Error leaving random matching queue: %s", type(exc).__name__)
        await callback.message.answer(MSG_GENERIC_ERROR)


def get_matching_router() -> Router:
    """Create and configure the random matching router."""
    router = Router(name="matching")

    router.message.register(handle_random_chat_menu_entry, F.text == BTN_RANDOM_CHAT)
    router.callback_query.register(handle_match_join_callback, F.data == CB_MATCH_JOIN)
    router.callback_query.register(handle_match_leave_callback, F.data == CB_MATCH_LEAVE)

    return router
