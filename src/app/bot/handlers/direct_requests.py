"""Telegram handlers for direct chat requests: send, inbox, sent, accept, reject."""

import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.app.bot.handlers.common import (
    MSG_GENERIC_ERROR,
    load_authorized_user,
    notify_chat_started,
    reply_with_auth_error,
)
from src.app.bot.keyboards.chat import (
    CB_DR_ACCEPT_PREFIX,
    CB_DR_CANCEL_PREFIX,
    CB_DR_INBOX,
    CB_DR_MENU,
    CB_DR_REJECT_PREFIX,
    CB_DR_SEND_BY_ID,
    CB_DR_SENT,
    get_direct_request_decision_keyboard,
    get_direct_requests_menu_keyboard,
    get_outgoing_request_keyboard,
)
from src.app.bot.keyboards.discovery import get_cancel_filter_input_keyboard
from src.app.bot.keyboards.onboarding import BTN_DIRECT_REQUEST, get_main_menu_keyboard
from src.app.bot.states import DirectRequestSG
from src.app.config.settings import Settings, get_settings
from src.app.infrastructure.database.session import DatabaseManager, get_database_manager
from src.app.modules.chat.direct_requests import DirectRequestService
from src.app.modules.notifications.service import NotificationService, format_public_profile_card

logger = logging.getLogger(__name__)


async def handle_direct_request_menu_entry(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Handle '✉️ درخواست مستقیم' main menu entry."""
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

        svc = DirectRequestService(resolved_db)
        inbox = await svc.list_incoming_requests(user.id)
        sent = await svc.list_sent_requests(user.id)

        await message.answer(
            "✉️ <b>مدیریت درخواست‌های گفتگوی مستقیم</b>\n\n"
            f"📥 درخواست‌های دریافتی در انتظار: <b>{len(inbox)}</b>\n"
            f"📤 درخواست‌های ارسالی در انتظار: <b>{len(sent)}</b>\n\n"
            "از طریق دکمه‌های زیر می‌توانید درخواست‌ها را بررسی کرده یا "
            "با شناسه عمومی درخواست جدید ارسال کنید:",
            reply_markup=get_direct_requests_menu_keyboard(),
        )
    except Exception as exc:
        logger.error("Error opening direct requests menu: %s", type(exc).__name__)
        await message.answer(MSG_GENERIC_ERROR)


async def handle_dr_inbox_callback(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Show incoming pending direct requests."""
    await callback.answer()
    if callback.from_user is None or not isinstance(callback.message, Message):
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(resolved_db, callback.from_user.id)
        if err is not None or user is None:
            await reply_with_auth_error(callback, err or MSG_GENERIC_ERROR)
            return

        inbox = await DirectRequestService(resolved_db).list_incoming_requests(user.id)
        if not inbox:
            await callback.message.answer(
                "📥 در حال حاضر هیچ درخواست گفتگوی در انتظاری ندارید.",
                reply_markup=get_direct_requests_menu_keyboard(),
            )
            return

        for req in inbox[:10]:
            card = format_public_profile_card(
                req.peer_profile,
                header="📥 <b>درخواست گفتگوی مستقیم دریافتی</b>",
            )
            await callback.message.answer(
                card,
                reply_markup=get_direct_request_decision_keyboard(req.id),
            )
    except Exception as exc:
        logger.error("Error listing incoming requests: %s", type(exc).__name__)
        await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_dr_sent_callback(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Show outgoing pending direct requests."""
    await callback.answer()
    if callback.from_user is None or not isinstance(callback.message, Message):
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(resolved_db, callback.from_user.id)
        if err is not None or user is None:
            await reply_with_auth_error(callback, err or MSG_GENERIC_ERROR)
            return

        sent = await DirectRequestService(resolved_db).list_sent_requests(user.id)
        if not sent:
            await callback.message.answer(
                "📤 در حال حاضر هیچ درخواست ارسالی در انتظاری ندارید.",
                reply_markup=get_direct_requests_menu_keyboard(),
            )
            return

        for req in sent[:10]:
            card = format_public_profile_card(
                req.peer_profile,
                header="📤 <b>درخواست گفتگوی ارسالی در انتظار پاسخ</b>",
            )
            await callback.message.answer(
                card,
                reply_markup=get_outgoing_request_keyboard(req.id),
            )
    except Exception as exc:
        logger.error("Error listing sent requests: %s", type(exc).__name__)
        await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_dr_send_by_id_prompt(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Prompt user for target user's public_id to send a direct request."""
    await callback.answer()
    await state.set_state(DirectRequestSG.waiting_for_target_public_id)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "🆔 لطفاً شناسه عمومی کاربر مورد نظر را وارد نمایید "
            "(مثال: <code>usr_abc123def456</code>):",
            reply_markup=get_cancel_filter_input_keyboard(),
        )


async def handle_dr_send_by_id_input(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Submit a direct chat request to a user-provided public_id."""
    if message.from_user is None:
        return

    target_pid = (message.text or "").strip()
    if not target_pid:
        await message.answer(
            "❌ شناسه عمومی نمی‌تواند خالی باشد.",
            reply_markup=get_cancel_filter_input_keyboard(),
        )
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(
            resolved_db,
            message.from_user.id,
            require_completed_profile=True,
            allow_chat_restricted=False,
            settings=resolved_settings,
        )
        if err is not None or user is None:
            await state.clear()
            await reply_with_auth_error(message, err or MSG_GENERIC_ERROR)
            return

        result = await DirectRequestService(resolved_db).send_direct_request(user.id, target_pid)
        await state.clear()

        escaped_target_pid = html.escape(result.target_profile.public_id)
        await message.answer(
            f"✅ درخواست گفتگوی مستقیم به کاربر <code>{escaped_target_pid}</code> ارسال شد.\n"
            "⏳ مهلت پاسخ‌گویی به این درخواست ۲۴ ساعت است.",
            reply_markup=get_main_menu_keyboard(),
        )

        notif_text = format_public_profile_card(
            result.requester_profile,
            header="✉️ <b>درخواست گفتگوی مستقیم جدید!</b>",
        )
        await NotificationService().send_safe_message(
            message.bot,
            result.target_telegram_id,
            f"{notif_text}\n\n⏳ این درخواست تا ۲۴ ساعت آینده معتبر است.",
            reply_markup=get_direct_request_decision_keyboard(result.request_id),
        )
    except (ValueError, PermissionError, LookupError, RuntimeError) as domain_err:
        await state.clear()
        await message.answer(
            f"❌ {html.escape(str(domain_err))}", reply_markup=get_main_menu_keyboard()
        )
    except Exception as exc:
        logger.error("Error sending direct request by public_id: %s", type(exc).__name__)
        await state.clear()
        await message.answer(MSG_GENERIC_ERROR)


async def handle_dr_decision_callback(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Handle accept or reject decision callback on a direct request."""
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    cb_data = callback.data or ""
    if cb_data.startswith(CB_DR_ACCEPT_PREFIX):
        accept = True
        raw_id = cb_data.replace(CB_DR_ACCEPT_PREFIX, "")
    elif cb_data.startswith(CB_DR_REJECT_PREFIX):
        accept = False
        raw_id = cb_data.replace(CB_DR_REJECT_PREFIX, "")
    else:
        await callback.answer()
        return

    try:
        request_id = int(raw_id)
    except ValueError:
        await callback.answer("درخواست نامعتبر است.")
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(
            resolved_db,
            callback.from_user.id,
            require_completed_profile=True,
            allow_chat_restricted=not accept,
            settings=resolved_settings,
        )
        if err is not None or user is None:
            await callback.answer()
            await reply_with_auth_error(callback, err or MSG_GENERIC_ERROR)
            return

        decision = await DirectRequestService(resolved_db).respond_to_direct_request(
            request_id=request_id,
            actor_user_id=user.id,
            accept=accept,
        )

        # Clear inline keyboard on request card to avoid double clicks
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        if decision.status == "REJECTED":
            await callback.answer("درخواست رد شد.")
            await callback.message.answer(
                "❌ درخواست گفتگو رد شد.",
                reply_markup=get_main_menu_keyboard(),
            )
            await NotificationService().send_safe_message(
                callback.bot,
                decision.requester_telegram_id,
                f"❌ درخواست گفتگوی مستقیم شما با کاربر "
                f"<code>{html.escape(decision.target_profile.public_id)}</code> پذیرفته نشد.",
            )
            return

        await callback.answer("درخواست پذیرفته شد ✅")
        assert decision.chat_session_id is not None

        await notify_chat_started(
            callback.bot,
            chat_id=decision.chat_session_id,
            user1_telegram_id=decision.requester_telegram_id,
            user1_sees_profile=decision.target_profile,
            user2_telegram_id=decision.target_telegram_id,
            user2_sees_profile=decision.requester_profile,
            intro_title="💬 <b>درخواست مستقیم پذیرفته شد و گفتگو آغاز گردید!</b>",
        )
    except (ValueError, PermissionError, LookupError, RuntimeError) as domain_err:
        await callback.answer(str(domain_err)[:180], show_alert=True)
    except Exception as exc:
        logger.error("Error deciding direct request: %s", type(exc).__name__)
        await callback.answer()
        await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_dr_cancel_callback(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Handle requester cancelling their own pending outgoing request."""
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    raw_id = (callback.data or "").replace(CB_DR_CANCEL_PREFIX, "")
    try:
        request_id = int(raw_id)
    except ValueError:
        await callback.answer("درخواست نامعتبر است.")
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(resolved_db, callback.from_user.id)
        if err is not None or user is None:
            await callback.answer()
            return

        cancelled = await DirectRequestService(resolved_db).cancel_outgoing_request(
            request_id, user.id
        )
        if not cancelled:
            await callback.answer("این درخواست دیگر قابل لغو نیست.", show_alert=True)
            return

        await callback.answer("درخواست لغو شد.")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer("✅ درخواست ارسالی لغو گردید.")
    except PermissionError as perm_err:
        await callback.answer(str(perm_err)[:180], show_alert=True)
    except Exception as exc:
        logger.error("Error cancelling direct request: %s", type(exc).__name__)
        await callback.answer()


def get_direct_requests_router() -> Router:
    """Create and configure the direct requests router."""
    router = Router(name="direct_requests")

    router.message.register(handle_direct_request_menu_entry, F.text == BTN_DIRECT_REQUEST)
    router.callback_query.register(handle_dr_inbox_callback, F.data == CB_DR_INBOX)
    router.callback_query.register(handle_dr_sent_callback, F.data == CB_DR_SENT)
    router.callback_query.register(handle_dr_send_by_id_prompt, F.data == CB_DR_SEND_BY_ID)
    router.callback_query.register(handle_dr_inbox_callback, F.data == CB_DR_MENU)
    router.message.register(
        handle_dr_send_by_id_input, DirectRequestSG.waiting_for_target_public_id
    )
    router.callback_query.register(
        handle_dr_decision_callback,
        F.data.startswith(CB_DR_ACCEPT_PREFIX) | F.data.startswith(CB_DR_REJECT_PREFIX),
    )
    router.callback_query.register(
        handle_dr_cancel_callback, F.data.startswith(CB_DR_CANCEL_PREFIX)
    )

    return router
