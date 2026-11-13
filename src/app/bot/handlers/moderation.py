"""Telegram bot handlers for user abuse reporting workflow."""

import html
import logging
import uuid

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.app.bot.handlers.common import (
    MSG_GENERIC_ERROR,
    load_authorized_user,
    reply_with_auth_error,
)
from src.app.bot.keyboards.moderation import (
    CB_REPORT_CANCEL,
    CB_REPORT_CAT_PREFIX,
    CB_REPORT_SKIP_DESC,
    CB_REPORT_START_CHAT_PREFIX,
    CB_REPORT_START_USER_PREFIX,
    get_report_category_keyboard,
    get_report_description_keyboard,
)
from src.app.bot.states import ReportSG
from src.app.config.settings import Settings, get_settings
from src.app.infrastructure.database.session import DatabaseManager, get_database_manager
from src.app.modules.moderation.schemas import MAX_REPORT_DESCRIPTION_LENGTH, REPORT_CATEGORIES
from src.app.modules.moderation.service import ReportService, validate_report_description

logger = logging.getLogger(__name__)


async def handle_report_start_chat_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Start abuse report flow scoped to a specific chat session."""
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    hex_id = (callback.data or "").replace(CB_REPORT_START_CHAT_PREFIX, "").strip()
    try:
        chat_id = uuid.UUID(hex=hex_id)
    except ValueError:
        await callback.answer("شناسه گفتگو نامعتبر است.", show_alert=True)
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(
            resolved_db, callback.from_user.id, settings=resolved_settings
        )
        if err is not None or user is None:
            await callback.answer()
            await reply_with_auth_error(callback, err or MSG_GENERIC_ERROR)
            return

        await callback.answer()
        await state.clear()
        await state.set_state(ReportSG.selecting_category)
        await state.update_data(
            report_mode="CHAT",
            chat_session_id=chat_id.hex,
            reported_public_id=None,
        )
        await callback.message.answer(
            "🚨 <b>ثبت گزارش تخلف گفتگو</b>\n\n"
            "لطفاً نوع تخلف یا مشکل مشاهده‌شده را از گزینه‌های زیر انتخاب فرمایید:",
            reply_markup=get_report_category_keyboard(),
        )
    except Exception as exc:
        logger.error("Error starting chat report flow: %s", type(exc).__name__)
        await callback.answer()
        await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_report_start_user_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Start abuse report flow targeting a user's public profile / public_id."""
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    target_pid = (callback.data or "").replace(CB_REPORT_START_USER_PREFIX, "").strip()
    if not target_pid:
        await callback.answer("شناسه کاربر نامعتبر است.", show_alert=True)
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(
            resolved_db, callback.from_user.id, settings=resolved_settings
        )
        if err is not None or user is None:
            await callback.answer()
            await reply_with_auth_error(callback, err or MSG_GENERIC_ERROR)
            return

        await callback.answer()
        await state.clear()
        await state.set_state(ReportSG.selecting_category)
        await state.update_data(
            report_mode="USER",
            reported_public_id=target_pid,
            chat_session_id=None,
        )
        await callback.message.answer(
            "🚨 <b>ثبت گزارش تخلف کاربر</b>\n\n"
            f"کاربر هدف: <code>{html.escape(target_pid)}</code>\n"
            "لطفاً دسته‌بندی تخلف را انتخاب فرمایید:",
            reply_markup=get_report_category_keyboard(),
        )
    except Exception as exc:
        logger.error("Error starting user report flow: %s", type(exc).__name__)
        await callback.answer()
        await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_report_category_callback(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Store selected category and ask for optional report description."""
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    category = (callback.data or "").replace(CB_REPORT_CAT_PREFIX, "").strip().upper()
    if category not in REPORT_CATEGORIES:
        await callback.answer("دسته‌بندی نامعتبر است.", show_alert=True)
        return

    await callback.answer()
    await state.update_data(category=category)
    await state.set_state(ReportSG.entering_description)

    cat_label = REPORT_CATEGORIES[category]
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await callback.message.answer(
        f"📌 دسته‌بندی انتخاب‌شده: <b>{html.escape(cat_label)}</b>\n\n"
        f"در صورت نیاز می‌توانید توضیح کوتاهی (حداکثر {MAX_REPORT_DESCRIPTION_LENGTH} کاراکتر) "
        "ارسال کنید یا دکمه «ثبت گزارش بدون توضیحات» را بزنید:",
        reply_markup=get_report_description_keyboard(),
    )


async def _finalize_report_submission(
    *,
    target_message: Message,
    state: FSMContext,
    reporter_telegram_id: int,
    description: str | None,
    db_manager: DatabaseManager,
    settings: Settings,
) -> None:
    data = await state.get_data()
    category = data.get("category")
    mode = data.get("report_mode")
    chat_hex = data.get("chat_session_id")
    target_pid = data.get("reported_public_id")

    if not category or mode not in {"CHAT", "USER"}:
        await state.clear()
        await target_message.answer("❌ اطلاعات گزارش ناقص است. لطفاً مجدداً اقدام کنید.")
        return

    user, err = await load_authorized_user(db_manager, reporter_telegram_id, settings=settings)
    if err is not None or user is None:
        await state.clear()
        await reply_with_auth_error(target_message, err or MSG_GENERIC_ERROR)
        return

    report_svc = ReportService(db_manager)
    if mode == "CHAT":
        chat_uuid = uuid.UUID(hex=chat_hex) if chat_hex else None
        result = await report_svc.submit_active_chat_report(
            reporter_user_id=user.id,
            chat_session_id=chat_uuid,
            category=category,
            description=description,
        )
    else:
        result = await report_svc.submit_report(
            reporter_user_id=user.id,
            reported_public_id=target_pid,
            category=category,
            description=description,
        )

    await state.clear()
    cat_label = REPORT_CATEGORIES.get(result.category, result.category)
    await target_message.answer(
        "✅ <b>گزارش تخلف شما با موفقیت ثبت شد.</b>\n\n"
        f"🔢 شماره پیگیری: <code>#{result.report_id}</code>\n"
        f"📂 موضوع: <b>{html.escape(cat_label)}</b>\n"
        f"👤 شناسه کاربر گزارش‌شده: <code>{html.escape(result.reported_public_id)}</code>\n\n"
        "🔍 این گزارش توسط تیم نظارت بررسی خواهد شد."
    )


async def handle_report_description_input(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Validate optional report text and persist the report."""
    if message.from_user is None:
        return

    if not message.text:
        await message.answer(
            "❌ لطفاً توضیحات خود را به صورت متن ارسال کنید "
            "یا دکمه «ثبت گزارش بدون توضیحات» را بزنید:",
            reply_markup=get_report_description_keyboard(),
        )
        return

    try:
        clean_desc = validate_report_description(message.text)
    except ValueError as val_err:
        await message.answer(
            f"❌ {html.escape(str(val_err))}",
            reply_markup=get_report_description_keyboard(),
        )
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        await _finalize_report_submission(
            target_message=message,
            state=state,
            reporter_telegram_id=message.from_user.id,
            description=clean_desc,
            db_manager=resolved_db,
            settings=resolved_settings,
        )
    except (ValueError, PermissionError, LookupError) as domain_err:
        await state.clear()
        await message.answer(f"❌ {html.escape(str(domain_err))}")
    except Exception as exc:
        logger.error("Error submitting report with description: %s", type(exc).__name__)
        await state.clear()
        await message.answer(MSG_GENERIC_ERROR)


async def handle_report_skip_description_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Submit the report immediately without optional description text."""
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        await callback.answer("در حال ثبت گزارش...")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await _finalize_report_submission(
            target_message=callback.message,
            state=state,
            reporter_telegram_id=callback.from_user.id,
            description=None,
            db_manager=resolved_db,
            settings=resolved_settings,
        )
    except (ValueError, PermissionError, LookupError) as domain_err:
        await state.clear()
        await callback.message.answer(f"❌ {html.escape(str(domain_err))}")
    except Exception as exc:
        logger.error("Error submitting report without description: %s", type(exc).__name__)
        await state.clear()
        await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_report_cancel_callback(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Cancel report creation and clear FSM state."""
    await callback.answer("ثبت گزارش لغو شد.")
    await state.clear()
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer("ثبت گزارش تخلف لغو شد.")


def get_moderation_router() -> Router:
    """Create and configure the user moderation reporting router."""
    router = Router(name="moderation")

    router.callback_query.register(
        handle_report_start_chat_callback,
        F.data.startswith(CB_REPORT_START_CHAT_PREFIX),
    )
    router.callback_query.register(
        handle_report_start_user_callback,
        F.data.startswith(CB_REPORT_START_USER_PREFIX),
    )
    router.callback_query.register(
        handle_report_cancel_callback,
        F.data == CB_REPORT_CANCEL,
    )
    router.callback_query.register(
        handle_report_category_callback,
        ReportSG.selecting_category,
        F.data.startswith(CB_REPORT_CAT_PREFIX),
    )
    router.callback_query.register(
        handle_report_skip_description_callback,
        ReportSG.entering_description,
        F.data == CB_REPORT_SKIP_DESC,
    )
    router.message.register(
        handle_report_description_input,
        ReportSG.entering_description,
    )

    return router
