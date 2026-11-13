"""Telegram handlers for managing personal connections and user blocks."""

import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.app.bot.handlers.common import (
    MSG_GENERIC_ERROR,
    load_authorized_user,
    reply_with_auth_error,
)
from src.app.bot.keyboards.chat import (
    CB_CONN_BLK_PREFIX,
    CB_CONN_LIST_PREFIX,
    CB_CONN_OPEN_PREFIX,
    CB_CONN_REM_PREFIX,
    CB_CONN_REQ_PREFIX,
    get_connection_detail_keyboard,
    get_connections_list_keyboard,
    get_direct_request_decision_keyboard,
)
from src.app.bot.keyboards.onboarding import BTN_MY_CONNECTIONS, get_main_menu_keyboard
from src.app.config.settings import Settings, get_settings
from src.app.infrastructure.database.session import DatabaseManager, get_database_manager
from src.app.modules.chat.connections import ConnectionAndBlockService
from src.app.modules.chat.direct_requests import DirectRequestService
from src.app.modules.discovery.service import DiscoveryService
from src.app.modules.notifications.service import NotificationService, format_public_profile_card

logger = logging.getLogger(__name__)


async def _render_connections_list(
    target: Message,
    db_manager: DatabaseManager,
    user_id: int,
    *,
    offset: int = 0,
) -> None:
    connections = await ConnectionAndBlockService(db_manager).list_connections(user_id)
    if not connections:
        await target.answer(
            "👥 <b>ارتباط‌های من</b>\n\n"
            "شما هنوز ارتباط ثبت‌شده‌ای ندارید. "
            "پس از گفتگو با کاربران، نام آن‌ها در این لیست قرار می‌گیرد.",
            reply_markup=get_main_menu_keyboard(),
        )
        return

    await target.answer(
        f"👥 <b>ارتباط‌های من ({len(connections)} نفر)</b>\n\n"
        "برای مشاهده پروفایل، ارسال درخواست گفتگو، حذف یا مسدودسازی "
        "یکی از موارد زیر را انتخاب کنید:",
        reply_markup=get_connections_list_keyboard(connections, offset=offset),
    )


async def handle_connections_menu_entry(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Handle '👥 ارتباطهای من' main menu button."""
    if message.from_user is None:
        return

    await state.clear()
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(resolved_db, message.from_user.id)
        if err is not None or user is None:
            await reply_with_auth_error(message, err or MSG_GENERIC_ERROR)
            return

        await _render_connections_list(message, resolved_db, user.id, offset=0)
    except Exception as exc:
        logger.error("Error listing connections: %s", type(exc).__name__)
        await message.answer(MSG_GENERIC_ERROR)


async def handle_connections_list_callback(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Paginate connections list."""
    await callback.answer()
    if callback.from_user is None or not isinstance(callback.message, Message):
        return

    raw_offset = (callback.data or "").replace(CB_CONN_LIST_PREFIX, "").strip()
    try:
        offset = max(0, int(raw_offset))
    except ValueError:
        offset = 0

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(resolved_db, callback.from_user.id)
        if err is not None or user is None:
            await reply_with_auth_error(callback, err or MSG_GENERIC_ERROR)
            return

        await _render_connections_list(callback.message, resolved_db, user.id, offset=offset)
    except Exception as exc:
        logger.error("Error paginating connections: %s", type(exc).__name__)
        await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_connection_open_callback(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Open a connected user's public profile respecting visibility & block rules."""
    await callback.answer()
    if callback.from_user is None or not isinstance(callback.message, Message):
        return

    target_pid = (callback.data or "").replace(CB_CONN_OPEN_PREFIX, "").strip()
    if not target_pid:
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(resolved_db, callback.from_user.id)
        if err is not None or user is None:
            await reply_with_auth_error(callback, err or MSG_GENERIC_ERROR)
            return

        profile = await DiscoveryService(resolved_db).get_public_profile_by_public_id(
            user.id, target_pid
        )
        if profile is None:
            await callback.message.answer(
                "⚠️ پروفایل این کاربر در حال حاضر در دسترس نیست (مخفی شده یا مسدود است).",
                reply_markup=get_connection_detail_keyboard(target_pid, allow_request=False),
            )
            return

        await callback.message.answer(
            format_public_profile_card(profile, header="👥 <b>پروفایل ارتباط</b>"),
            reply_markup=get_connection_detail_keyboard(
                profile.public_id, allow_request=not profile.in_active_chat
            ),
        )
    except Exception as exc:
        logger.error("Error opening connection profile: %s", type(exc).__name__)
        await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_connection_request_callback(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Send direct chat request to a connection."""
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    target_pid = (callback.data or "").replace(CB_CONN_REQ_PREFIX, "").strip()
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(
            resolved_db,
            callback.from_user.id,
            require_completed_profile=True,
            allow_chat_restricted=False,
            settings=resolved_settings,
        )
        if err is not None or user is None:
            await callback.answer()
            await reply_with_auth_error(callback, err or MSG_GENERIC_ERROR)
            return

        result = await DirectRequestService(resolved_db).send_direct_request(user.id, target_pid)
        await callback.answer("درخواست گفتگو ارسال شد ✅")
        escaped_target_pid = html.escape(result.target_profile.public_id)
        await callback.message.answer(
            f"✅ درخواست گفتگوی مستقیم به کاربر <code>{escaped_target_pid}</code> ارسال شد."
        )

        notif_text = format_public_profile_card(
            result.requester_profile,
            header="✉️ <b>درخواست گفتگوی مستقیم جدید!</b>",
        )
        await NotificationService().send_safe_message(
            callback.bot,
            result.target_telegram_id,
            f"{notif_text}\n\n⏳ این درخواست تا ۲۴ ساعت آینده معتبر است.",
            reply_markup=get_direct_request_decision_keyboard(result.request_id),
        )
    except (ValueError, PermissionError, LookupError, RuntimeError) as domain_err:
        await callback.answer(str(domain_err)[:180], show_alert=True)
    except Exception as exc:
        logger.error("Error sending request from connection: %s", type(exc).__name__)
        await callback.answer()
        await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_connection_remove_callback(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Asymmetrically remove a connection from the acting user's list only."""
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    target_pid = (callback.data or "").replace(CB_CONN_REM_PREFIX, "").strip()
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(resolved_db, callback.from_user.id)
        if err is not None or user is None:
            await callback.answer()
            return

        removed = await ConnectionAndBlockService(resolved_db).remove_connection(
            user.id, target_pid
        )
        if not removed:
            await callback.answer("این ارتباط قبلاً حذف شده است.", show_alert=True)
            return

        await callback.answer("از لیست ارتباط‌ها حذف شد.")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer(
            f"🗑 کاربر <code>{html.escape(target_pid)}</code> فقط از لیست ارتباط‌های شما حذف شد.",
            reply_markup=get_main_menu_keyboard(),
        )
    except Exception as exc:
        logger.error("Error removing connection: %s", type(exc).__name__)
        await callback.answer()
        await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_connection_block_callback(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Block a user from connections screen."""
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    target_pid = (callback.data or "").replace(CB_CONN_BLK_PREFIX, "").strip()
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(resolved_db, callback.from_user.id)
        if err is not None or user is None:
            await callback.answer()
            return

        result = await ConnectionAndBlockService(resolved_db).block_user(
            user.id, target_pid, reason="CONNECTION_BLOCK"
        )
        await callback.answer("کاربر مسدود شد 🚫")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer(
            f"🚫 کاربر <code>{html.escape(target_pid)}</code> مسدود شد و "
            "دیگر امکان برقراری ارتباط وجود نخواهد داشت.",
            reply_markup=get_main_menu_keyboard(),
        )
        if result.terminated_partner_telegram_id:
            await NotificationService().send_safe_message(
                callback.bot,
                result.terminated_partner_telegram_id,
                "⚠️ گفتگوی فعال شما پایان یافت.",
                reply_markup=get_main_menu_keyboard(),
            )
    except (ValueError, PermissionError, LookupError) as domain_err:
        await callback.answer(str(domain_err)[:180], show_alert=True)
    except Exception as exc:
        logger.error("Error blocking connection user: %s", type(exc).__name__)
        await callback.answer()
        await callback.message.answer(MSG_GENERIC_ERROR)


def get_connections_router() -> Router:
    """Create and configure the connections management router."""
    router = Router(name="connections")

    router.message.register(handle_connections_menu_entry, F.text == BTN_MY_CONNECTIONS)
    router.callback_query.register(
        handle_connections_list_callback, F.data.startswith(CB_CONN_LIST_PREFIX)
    )
    router.callback_query.register(
        handle_connection_open_callback, F.data.startswith(CB_CONN_OPEN_PREFIX)
    )
    router.callback_query.register(
        handle_connection_request_callback, F.data.startswith(CB_CONN_REQ_PREFIX)
    )
    router.callback_query.register(
        handle_connection_remove_callback, F.data.startswith(CB_CONN_REM_PREFIX)
    )
    router.callback_query.register(
        handle_connection_block_callback, F.data.startswith(CB_CONN_BLK_PREFIX)
    )

    return router
