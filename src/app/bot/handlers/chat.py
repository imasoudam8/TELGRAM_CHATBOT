"""Telegram handlers for active anonymous chat relay, termination, and blocking."""

import html
import logging
import uuid

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.app.bot.filters.active_chat import ActiveChatFilter
from src.app.bot.handlers.common import (
    MSG_GENERIC_ERROR,
    load_authorized_user,
    reply_with_auth_error,
)
from src.app.bot.keyboards.chat import (
    BTN_CHAT_BLOCK,
    BTN_END_CHAT,
    CB_CHAT_BLK_PREFIX,
    CB_CHAT_END_PREFIX,
    get_active_chat_reply_keyboard,
)
from src.app.bot.keyboards.onboarding import MAIN_MENU_BUTTONS, get_main_menu_keyboard
from src.app.config.settings import Settings, get_settings
from src.app.infrastructure.database.session import DatabaseManager, get_database_manager
from src.app.modules.chat.connections import ConnectionAndBlockService
from src.app.modules.chat.session_service import (
    VIDEO_SIZE_LIMIT_BYTES,
    ActiveChatInfo,
    ChatService,
    RelayMessageResult,
)
from src.app.modules.notifications.service import NotificationService
from src.app.modules.wallet.settlement import ChatSettlementService

logger = logging.getLogger(__name__)


async def _deliver_relayed_message(bot: Bot | None, result: RelayMessageResult) -> bool:
    """Relay text or media by Telegram file_id cleanly without exposing sender identity."""
    if bot is None:
        return False

    try:
        escaped_caption = html.escape(result.caption) if result.caption else None
        if result.message_type == "TEXT":
            await bot.send_message(
                chat_id=result.recipient_telegram_id,
                text=html.escape(result.text or ""),
            )
        elif result.message_type == "PHOTO" and result.telegram_file_id:
            await bot.send_photo(
                chat_id=result.recipient_telegram_id,
                photo=result.telegram_file_id,
                caption=escaped_caption,
            )
        elif result.message_type == "VIDEO" and result.telegram_file_id:
            await bot.send_video(
                chat_id=result.recipient_telegram_id,
                video=result.telegram_file_id,
                caption=escaped_caption,
            )
        elif result.message_type == "VOICE" and result.telegram_file_id:
            await bot.send_voice(
                chat_id=result.recipient_telegram_id,
                voice=result.telegram_file_id,
                caption=escaped_caption,
            )
        elif result.message_type == "STICKER" and result.telegram_file_id:
            await bot.send_sticker(
                chat_id=result.recipient_telegram_id,
                sticker=result.telegram_file_id,
            )
        elif result.message_type == "ANIMATION" and result.telegram_file_id:
            await bot.send_animation(
                chat_id=result.recipient_telegram_id,
                animation=result.telegram_file_id,
                caption=escaped_caption,
            )
        else:
            return False
        return True
    except Exception as exc:
        logger.warning(
            "Telegram relay delivery failed (%s) for chat=%s",
            type(exc).__name__,
            result.chat_id,
        )
        return False


def _extract_payload(
    message: Message,
) -> tuple[str | None, str | None, str | None, int | None, int | None, str | None]:
    """Extract (message_type, text, file_id, file_size_bytes, duration, caption)."""
    if message.text is not None:
        return "TEXT", message.text, None, None, None, None
    if message.photo:
        largest = message.photo[-1]
        return "PHOTO", None, largest.file_id, largest.file_size, None, message.caption
    if message.voice:
        v = message.voice
        return "VOICE", None, v.file_id, v.file_size, v.duration, message.caption
    if message.video:
        vid = message.video
        return "VIDEO", None, vid.file_id, vid.file_size, vid.duration, message.caption
    if message.sticker:
        st = message.sticker
        return "STICKER", None, st.file_id, st.file_size, None, None
    if message.animation:
        anim = message.animation
        return "ANIMATION", None, anim.file_id, anim.file_size, anim.duration, message.caption
    return None, None, None, None, None, None


async def handle_end_chat_message(
    message: Message,
    state: FSMContext,
    active_chat: ActiveChatInfo | None = None,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """End active anonymous chat via reply button or /end command."""
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

        result = await ChatService(resolved_db, resolved_settings).end_chat(
            user.id,
            expected_chat_id=active_chat.chat_id if active_chat else None,
            reason="USER_LEFT",
        )
        if result is None:
            await message.answer(
                "در حال حاضر گفتگوی فعالی ندارید.",
                reply_markup=get_main_menu_keyboard(),
            )
            return

        await message.answer(
            "✅ گفتگوی ناشناس شما به پایان رسید.",
            reply_markup=get_main_menu_keyboard(),
        )
        await NotificationService().send_safe_message(
            message.bot,
            result.partner_telegram_id,
            "ℹ️ مخاطب شما گفتگو را پایان داد.",
            reply_markup=get_main_menu_keyboard(),
        )
    except Exception as exc:
        logger.error("Error ending chat via message: %s", type(exc).__name__)
        await message.answer(MSG_GENERIC_ERROR)


async def handle_end_chat_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """End active anonymous chat via session-scoped inline button."""
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    hex_id = (callback.data or "").replace(CB_CHAT_END_PREFIX, "").strip()
    try:
        expected_chat_id = uuid.UUID(hex=hex_id)
    except ValueError:
        await callback.answer("دکمه نامعتبر است.")
        return

    await state.clear()
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(resolved_db, callback.from_user.id)
        if err is not None or user is None:
            await callback.answer()
            return

        result = await ChatService(resolved_db, resolved_settings).end_chat(
            user.id, expected_chat_id=expected_chat_id, reason="USER_LEFT"
        )
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        if result is None:
            await callback.answer("این گفتگو قبلاً پایان یافته است.")
            return

        await callback.answer("گفتگو پایان یافت.")
        await callback.message.answer(
            "✅ گفتگوی ناشناس شما به پایان رسید.",
            reply_markup=get_main_menu_keyboard(),
        )
        await NotificationService().send_safe_message(
            callback.bot,
            result.partner_telegram_id,
            "ℹ️ مخاطب شما گفتگو را پایان داد.",
            reply_markup=get_main_menu_keyboard(),
        )
    except Exception as exc:
        logger.error("Error ending chat via callback: %s", type(exc).__name__)
        await callback.answer()
        await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_block_partner_message(
    message: Message,
    state: FSMContext,
    active_chat: ActiveChatInfo | None = None,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Block current active chat partner via reply button and terminate chat."""
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

        chat_svc = ChatService(resolved_db)
        resolved_chat = active_chat or await chat_svc.get_active_chat_by_user_id(user.id)
        if resolved_chat is None:
            await message.answer("گفتگوی فعالی یافت نشد.", reply_markup=get_main_menu_keyboard())
            return

        blk_result = await ConnectionAndBlockService(
            resolved_db, resolved_settings
        ).block_active_chat_partner(user.id, resolved_chat.chat_id)
        await message.answer(
            "🚫 مخاطب مسدود شد و گفتگو بلافاصله پایان یافت.",
            reply_markup=get_main_menu_keyboard(),
        )
        if blk_result.terminated_partner_telegram_id:
            await NotificationService().send_safe_message(
                message.bot,
                blk_result.terminated_partner_telegram_id,
                "⚠️ گفتگوی فعال شما پایان یافت.",
                reply_markup=get_main_menu_keyboard(),
            )
    except Exception as exc:
        logger.error("Error blocking partner from chat message: %s", type(exc).__name__)
        await message.answer(MSG_GENERIC_ERROR)


async def handle_block_partner_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Block active chat partner via session-scoped inline button."""
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    hex_id = (callback.data or "").replace(CB_CHAT_BLK_PREFIX, "").strip()
    try:
        chat_id = uuid.UUID(hex=hex_id)
    except ValueError:
        await callback.answer("دکمه نامعتبر است.")
        return

    await state.clear()
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(resolved_db, callback.from_user.id)
        if err is not None or user is None:
            await callback.answer()
            return

        blk_result = await ConnectionAndBlockService(
            resolved_db, resolved_settings
        ).block_active_chat_partner(user.id, chat_id)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await callback.answer("کاربر مسدود شد.")
        await callback.message.answer(
            "🚫 مخاطب مسدود شد و گفتگو بلافاصله پایان یافت.",
            reply_markup=get_main_menu_keyboard(),
        )
        if blk_result.terminated_partner_telegram_id:
            await NotificationService().send_safe_message(
                callback.bot,
                blk_result.terminated_partner_telegram_id,
                "⚠️ گفتگوی فعال شما پایان یافت.",
                reply_markup=get_main_menu_keyboard(),
            )
    except LookupError:
        await callback.answer("این گفتگو دیگر فعال نیست.", show_alert=True)
    except Exception as exc:
        logger.error("Error blocking partner via callback: %s", type(exc).__name__)
        await callback.answer()
        await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_menu_button_while_in_chat(
    message: Message,
    active_chat: ActiveChatInfo | None = None,
) -> None:
    """Prevent accidental menu navigation from silently leaking into chat messages."""
    await message.answer(
        "💬 شما هم‌اکنون در یک گفتگوی ناشناس فعال هستید.\n"
        "برای استفاده از منوی اصلی، ابتدا گفتگوی فعلی را پایان دهید:",
        reply_markup=get_active_chat_reply_keyboard(),
    )


async def handle_chat_relay_message(
    message: Message,
    active_chat: ActiveChatInfo | None = None,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Relay validated text and media messages anonymously inside active chat."""
    if message.from_user is None:
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    msg_type, text_val, file_id, file_size, duration, caption = _extract_payload(message)
    if msg_type is None:
        await message.answer(
            "❌ ارسال این نوع فایل یا پیام (موسیقی، فایل سند، موقعیت مکانی یا مخاطب) "
            "در گفتگوی ناشناس پشتیبانی نمی‌شود.\n"
            "می‌توانید متن، عکس، ویدیو (کمتر از ۱۰ مگابایت)، پیام صوتی، استیکر یا گیف ارسال کنید."
        )
        return

    if msg_type == "VIDEO" and (file_size is None or file_size >= VIDEO_SIZE_LIMIT_BYTES):
        await message.answer(
            "❌ حجم ویدیو باید کمتر از ۱۰ مگابایت باشد. ویدیوهای ۱۰ مگابایت یا بیشتر ارسال نمی‌شوند."
        )
        return

    try:
        user, err = await load_authorized_user(resolved_db, message.from_user.id)
        if err is not None or user is None:
            await reply_with_auth_error(message, err or MSG_GENERIC_ERROR)
            return

        chat_svc = ChatService(resolved_db, resolved_settings)
        relay_result = await chat_svc.prepare_and_record_message(
            sender_user_id=user.id,
            message_type=msg_type,
            text_content=text_val,
            telegram_file_id=file_id,
            file_size_bytes=file_size,
            duration_seconds=duration,
            caption=caption,
        )

        delivered = await _deliver_relayed_message(message.bot, relay_result)
        if not delivered:
            if relay_result.sender_first_message_flag_set:
                await chat_svc.revert_first_message_flag(relay_result.chat_id, user.id)
            await message.answer(
                "⚠️ ارسال پیام به مخاطب انجام نشد. در صورت تکرار، می‌توانید گفتگو را پایان دهید."
            )
            return

        if relay_result.both_participants_sent:
            try:
                await ChatSettlementService(resolved_db, resolved_settings).settle_chat(
                    relay_result.chat_id
                )
            except Exception as settle_exc:
                logger.error(
                    "Chat settlement deferred due to error (%s) for chat=%s",
                    type(settle_exc).__name__,
                    relay_result.chat_id,
                )
    except PermissionError as safety_err:
        await message.answer(f"🚫 {html.escape(str(safety_err))}")
    except ValueError as val_err:
        await message.answer(f"❌ {html.escape(str(val_err))}")
    except (LookupError, RuntimeError) as state_err:
        await message.answer(
            f"ℹ️ {html.escape(str(state_err))}",
            reply_markup=get_main_menu_keyboard(),
        )
    except Exception as exc:
        logger.error("Error relaying chat message: %s", type(exc).__name__)
        await message.answer(MSG_GENERIC_ERROR)


def get_chat_router() -> Router:
    """Create and configure the active anonymous chat router."""
    router = Router(name="chat")

    # Session-scoped inline buttons
    router.callback_query.register(handle_end_chat_callback, F.data.startswith(CB_CHAT_END_PREFIX))
    router.callback_query.register(
        handle_block_partner_callback, F.data.startswith(CB_CHAT_BLK_PREFIX)
    )

    # End / Block commands or reply buttons while in active chat
    router.message.register(
        handle_end_chat_message,
        ActiveChatFilter(),
        (F.text == BTN_END_CHAT) | Command("end") | Command("endchat"),
    )
    router.message.register(
        handle_block_partner_message,
        ActiveChatFilter(),
        F.text == BTN_CHAT_BLOCK,
    )

    # Guard main menu taps while in active chat
    router.message.register(
        handle_menu_button_while_in_chat,
        ActiveChatFilter(),
        F.text.in_(MAIN_MENU_BUTTONS),
    )

    # Relay all remaining messages while in active chat
    router.message.register(handle_chat_relay_message, ActiveChatFilter())

    return router
