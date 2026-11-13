"""Handlers for user onboarding, adult self-declaration, and Persian main menu."""

import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from src.app.bot.keyboards.onboarding import (
    BTN_DIRECT_REQUEST,
    BTN_HELP_SUPPORT,
    BTN_MY_CONNECTIONS,
    BTN_MY_PROFILE,
    BTN_RANDOM_CHAT,
    BTN_VIEW_PROFILES,
    CALLBACK_AGE_CONFIRM,
    CALLBACK_AGE_DECLINE,
    MAIN_MENU_BUTTONS,
    get_age_declaration_keyboard,
    get_main_menu_keyboard,
)
from src.app.config.settings import Settings, get_settings
from src.app.infrastructure.database.session import DatabaseManager, get_database_manager
from src.app.modules.users.service import UserService

logger = logging.getLogger(__name__)

# Persian localized user-facing copy
WELCOME_TEXT = (
    "سلام! به ربات گفتگوی ناشناس خوش آمدید. 🌹\n\n"
    "این سامانه بستری برای ارتباط و گفتگوی کاربران بزرگسال است.\n"
    "استفاده از این ربات مستلزم داشتن حداقل ۱۸ سال تمام و تأیید شرایط سنی می‌باشد.\n\n"
    "لطفاً وضعیت سنی خود را اعلام فرمایید:"
)

UNDERAGE_TEXT = (
    "متأسفانه استفاده از خدمات این ربات تنها برای افراد بالای ۱۸ سال مجاز است.\n\n"
    "به منظور رعایت قوانین و ضوابط، دسترسی شما امکان‌پذیر نمی‌باشد. "
    "از درک و همراهی شما سپاسگزاریم."
)

REGISTRATION_SUCCESS_TEXT = (
    "✅ <b>ثبت‌نام شما با موفقیت انجام شد.</b>\n\n"
    "شناسه عمومی و ناشناس شما در سامانه:\n"
    "<code>{public_id}</code>\n\n"
    "🔒 هویت تلگرام شما برای سایر کاربران کاملاً مخفی و ناشناس باقی خواهد ماند.\n"
    "از منوی زیر می‌توانید به بخش‌های ربات دسترسی پیدا کنید:"
)

ALREADY_REGISTERED_TEXT = (
    "خوش آمدید! شما قبلاً ثبت‌نام کرده‌اید. 🌹\n\n"
    "شناسه ناشناس شما:\n"
    "<code>{public_id}</code>\n\n"
    "لطفاً گزینه مورد نظر خود را از منوی زیر انتخاب نمایید:"
)

FEATURE_COMING_SOON_TEXT = (
    "بخش «{feature_name}» در حال توسعه است و در به‌روزرسانی‌های آینده فعال خواهد شد. ✨"
)

HELP_TEXT = (
    "🆘 <b>راهنما و پشتیبانی</b>\n\n"
    "• این سامانه یک بستر گفتگوی امن و ناشناس است.\n"
    "• هیچ‌یک از اطلاعات هویتی و شماره تماس شما به کاربران نمایش داده نمی‌شود.\n"
    "• ارتباط با سایر افراد تنها از طریق شناسه عمومی یکتای شما انجام می‌گیرد.\n\n"
    "سایر قابلیت‌های سامانه به زودی در دسترس قرار خواهند گرفت."
)

UNREGISTERED_ACCESS_DENIED_TEXT = (
    "⚠️ برای استفاده از امکانات سامانه، ابتدا باید شرایط سنی ۱۸+ سال را تأیید نمایید.\n\n"
    "لطفاً از طریق دکمه زیر سن قانونی خود را مشخص کنید:"
)

DATABASE_ERROR_TEXT = (
    "متأسفانه در پردازش درخواست شما خطایی رخ داده است. لطفاً لحظاتی بعد مجدداً تلاش نمایید."
)


async def handle_start(
    message: Message,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Handle /start command.

    If the user has already declared 18+ and is registered, show the main menu.
    Otherwise, show the Persian welcome message and 18+ declaration prompt.
    """
    if message.from_user is None:
        return

    telegram_user_id = message.from_user.id
    settings = settings or get_settings()
    db_manager = db_manager or get_database_manager(settings)
    user_service = UserService(db_manager)

    try:
        user = await user_service.get_user_by_telegram_id(telegram_user_id)
        if user is not None and user.age_18_plus_declared:
            logger.info("Existing registered user %s accessed /start", telegram_user_id)
            await message.answer(
                ALREADY_REGISTERED_TEXT.format(public_id=user.public_id),
                reply_markup=get_main_menu_keyboard(),
            )
            return

        logger.info(
            "Unregistered or undeclared user %s prompt with age declaration",
            telegram_user_id,
        )
        await message.answer(
            WELCOME_TEXT,
            reply_markup=get_age_declaration_keyboard(),
        )
    except Exception as exc:
        logger.error("Error handling /start for user %s: %s", telegram_user_id, exc)
        await message.answer(DATABASE_ERROR_TEXT)


async def handle_age_confirm_callback(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Handle affirmative 18+ self-declaration callback.

    Registers the user in PostgreSQL, generates a random public ID,
    acknowledges the callback query, and shows the main menu.
    """
    await callback.answer("شرایط سنی تأیید شد.")

    if callback.from_user is None:
        return

    telegram_user_id = callback.from_user.id
    settings = settings or get_settings()
    db_manager = db_manager or get_database_manager(settings)
    user_service = UserService(db_manager)

    try:
        user, _is_new = await user_service.register_adult_user(
            telegram_user_id=telegram_user_id,
            business_tz=settings.business_timezone,
        )

        # Clear inline keyboard on original message to prevent duplicate clicks
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

            await callback.message.answer(
                REGISTRATION_SUCCESS_TEXT.format(public_id=user.public_id),
                reply_markup=get_main_menu_keyboard(),
            )

    except Exception as exc:
        logger.error("Error during age confirmation for user %s: %s", telegram_user_id, exc)
        if isinstance(callback.message, Message):
            await callback.message.answer(DATABASE_ERROR_TEXT)


async def handle_age_decline_callback(
    callback: CallbackQuery,
) -> None:
    """Handle underage declaration callback.

    Acknowledges callback query, informs the user politely in Persian that the
    service is only for adults, and does NOT grant access to the main menu.
    """
    await callback.answer()

    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            UNDERAGE_TEXT,
            reply_markup=None,
        )


async def handle_main_menu_entry(
    message: Message,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Handle Persian main menu button interactions.

    Guarantees users without age declaration cannot access registered features.
    Provides polite notification for features scheduled for subsequent tasks.
    """
    if message.from_user is None:
        return

    telegram_user_id = message.from_user.id
    settings = settings or get_settings()
    db_manager = db_manager or get_database_manager(settings)
    user_service = UserService(db_manager)

    try:
        user = await user_service.get_user_by_telegram_id(telegram_user_id)
        if user is None or not user.age_18_plus_declared:
            logger.warning(
                "Unregistered user %s attempted to access main menu button '%s'",
                telegram_user_id,
                message.text,
            )
            await message.answer(
                UNREGISTERED_ACCESS_DENIED_TEXT,
                reply_markup=get_age_declaration_keyboard(),
            )
            return

        button_text = message.text or ""
        if button_text == BTN_HELP_SUPPORT:
            await message.answer(HELP_TEXT, reply_markup=get_main_menu_keyboard())
        else:
            await message.answer(
                FEATURE_COMING_SOON_TEXT.format(feature_name=button_text),
                reply_markup=get_main_menu_keyboard(),
            )

    except Exception as exc:
        logger.error("Error handling menu button for user %s: %s", telegram_user_id, exc)
        await message.answer(DATABASE_ERROR_TEXT)


def get_onboarding_router() -> Router:
    """Create and configure the onboarding and main menu router."""
    router = Router(name="onboarding")
    router.message.register(handle_start, CommandStart())
    router.callback_query.register(handle_age_confirm_callback, F.data == CALLBACK_AGE_CONFIRM)
    router.callback_query.register(handle_age_decline_callback, F.data == CALLBACK_AGE_DECLINE)
    # Exclude buttons handled by dedicated domain routers
    dedicated_buttons = {
        BTN_RANDOM_CHAT,
        BTN_VIEW_PROFILES,
        BTN_DIRECT_REQUEST,
        BTN_MY_CONNECTIONS,
        BTN_MY_PROFILE,
    }
    other_menu_buttons = [btn for btn in MAIN_MENU_BUTTONS if btn not in dedicated_buttons]
    router.message.register(handle_main_menu_entry, F.text.in_(other_menu_buttons))
    return router
