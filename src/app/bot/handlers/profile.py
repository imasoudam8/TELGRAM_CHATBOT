"""Handlers for user profile creation, inspection, and editing."""

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.app.bot.keyboards.onboarding import (
    BTN_MY_PROFILE,
    get_age_declaration_keyboard,
    get_main_menu_keyboard,
)
from src.app.bot.keyboards.profile import (
    CB_BACK_TO_PROFILE,
    CB_CANCEL_WIZARD,
    CB_CONFIRM_CREATE,
    CB_EDIT_AGE,
    CB_EDIT_BIO,
    CB_EDIT_CITY,
    CB_EDIT_GENDER,
    CB_EDIT_NAME,
    CB_GENDER_PREFIX,
    CB_SKIP_BIO,
    CB_SKIP_CITY,
    CB_START_CREATE,
    GENDER_LABELS,
    get_cancel_edit_keyboard,
    get_gender_selection_keyboard,
    get_no_profile_keyboard,
    get_review_keyboard,
    get_skip_keyboard,
    get_view_profile_keyboard,
)
from src.app.bot.states import ProfileCreationSG, ProfileEditSG
from src.app.config.settings import Settings, get_settings
from src.app.infrastructure.database.session import DatabaseManager, get_database_manager
from src.app.modules.profiles.service import (
    ProfileData,
    ProfileService,
    validate_age,
    validate_bio,
    validate_city,
    validate_display_name,
    validate_gender,
)
from src.app.modules.users.models import UserProfile
from src.app.modules.users.service import UserService

logger = logging.getLogger(__name__)

# Persian localized copy
MSG_UNREGISTERED_DENIED = (
    "⚠️ برای مشاهده و ایجاد پروفایل، ابتدا باید شرایط سنی ۱۸+ سال را تأیید نمایید.\n\n"
    "لطفاً از طریق دکمه زیر وضعیت سنی خود را مشخص فرمایید:"
)

MSG_NO_PROFILE = (
    "👤 <b>شما هنوز پروفایلی ایجاد نکرده‌اید!</b>\n\n"
    "با تکمیل پروفایل، سایر کاربران می‌توانند با شما در محیطی امن و ناشناس آشنا شوند.\n\n"
    "✨ <i>حریم خصوصی: نام کاربری یا شماره تلگرام شما هرگز ذخیره یا نمایش داده نمی‌شود.</i>\n\n"
    "آیا مایلید هم‌اکنون پروفایل خود را بسازید؟"
)

MSG_ASK_NAME = (
    "📝 <b>مرحله ۱ از ۵: نام نمایشی</b>\n\n"
    "لطفاً یک نام نمایشی یا مستعار برای خود وارد کنید (بین ۲ تا ۵۰ کاراکتر):"
)

MSG_ASK_AGE = (
    "🎂 <b>مرحله ۲ از ۵: سن</b>\n\n"
    "لطفاً سن خود را به صورت عدد وارد کنید (بین ۱۸ تا ۹۹ سال):\n\n"
    "🔒 <i>توجه: تاریخ تولد دریافت یا ذخیره نمی‌شود و تنها عدد سن شما ثبت می‌گردد.</i>"
)

MSG_ASK_GENDER = (
    "⚧ <b>مرحله ۳ از ۵: جنسیت</b>\n\nلطفاً جنسیت خود را از میان گزینه‌های زیر انتخاب نمایید:"
)

MSG_ASK_CITY = (
    "🏙 <b>مرحله ۴ از ۵: شهر محل سکونت (اختیاری)</b>\n\n"
    "نام شهر خود را ارسال کنید، یا برای گذر از این مرحله دکمه «رد کردن» را بزنید:"
)

MSG_ASK_BIO = (
    "✍️ <b>مرحله ۵ از ۵: درباره من (اختیاری)</b>\n\n"
    "توضیح مختصری درباره علایق یا معرفی خود بنویسید (حداکثر ۵۰۰ کاراکتر)، "
    "یا دکمه «رد کردن» را بزنید:"
)

MSG_DATABASE_ERROR = (
    "متأسفانه در پردازش درخواست شما خطایی رخ داده است. لطفاً لحظاتی بعد مجدداً تلاش نمایید."
)


def format_profile_view(profile: UserProfile, is_new: bool = False) -> str:
    """Format user profile for display in Persian."""
    gender_persian = GENDER_LABELS.get(profile.gender, profile.gender)
    city_text = profile.city if profile.city else "<i>ثبت نشده</i>"
    bio_text = profile.bio if profile.bio else "<i>ثبت نشده</i>"
    status_text = "✅ تکمیل شده" if profile.completion_status == "COMPLETED" else "⏳ ناقص"

    title = (
        "✅ <b>پروفایل شما با موفقیت ایجاد شد!</b>\n\n" if is_new else "👤 <b>پروفایل من</b>\n\n"
    )

    return (
        f"{title}"
        f"🏷 <b>نام نمایشی:</b> {profile.display_name}\n"
        f"🎂 <b>سن:</b> {profile.age} سال\n"
        f"⚧ <b>جنسیت:</b> {gender_persian}\n"
        f"🏙 <b>شهر:</b> {city_text}\n"
        f"✍️ <b>درباره من:</b>\n{bio_text}\n\n"
        f"📊 <b>وضعیت پروفایل:</b> {status_text}\n"
        f"🔒 <i>شناسه کاربری تلگرام شما کاملاً محفوظ است.</i>"
    )


def format_review_summary(data: dict) -> str:
    """Format entered profile data before final confirmation."""
    gender_persian = GENDER_LABELS.get(data.get("gender", ""), data.get("gender", ""))
    city_text = data.get("city") or "<i>ثبت نشده</i>"
    bio_text = data.get("bio") or "<i>ثبت نشده</i>"

    return (
        "🔍 <b>پیش‌نمایش و تأیید نهایی پروفایل</b>\n\n"
        f"🏷 <b>نام نمایشی:</b> {data.get('display_name')}\n"
        f"🎂 <b>سن:</b> {data.get('age')} سال\n"
        f"⚧ <b>جنسیت:</b> {gender_persian}\n"
        f"🏙 <b>شهر:</b> {city_text}\n"
        f"✍️ <b>درباره من:</b>\n{bio_text}\n\n"
        "آیا اطلاعات فوق مورد تأیید شماست؟"
    )


# =============================================================================
# Entry Point: Menu Button "👤 پروفایل من"
# =============================================================================
async def handle_profile_menu_entry(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Entry point when user taps '👤 پروفایل من' in the main menu."""
    if message.from_user is None:
        return

    telegram_user_id = message.from_user.id
    settings = settings or get_settings()
    db_manager = db_manager or get_database_manager(settings)
    user_service = UserService(db_manager)
    profile_service = ProfileService(db_manager)

    # Clear any lingering FSM state
    await state.clear()

    try:
        user = await user_service.get_user_by_telegram_id(telegram_user_id)
        if user is None or not user.age_18_plus_declared:
            await message.answer(
                MSG_UNREGISTERED_DENIED,
                reply_markup=get_age_declaration_keyboard(),
            )
            return

        profile = await profile_service.get_profile_by_telegram_id(telegram_user_id)
        if profile is None:
            await message.answer(
                MSG_NO_PROFILE,
                reply_markup=get_no_profile_keyboard(),
            )
            return

        # Show existing profile
        await message.answer(
            format_profile_view(profile, is_new=False),
            reply_markup=get_view_profile_keyboard(),
        )

    except Exception as exc:
        logger.error("Error accessing profile for user %s: %s", telegram_user_id, exc)
        await message.answer(MSG_DATABASE_ERROR)


# =============================================================================
# Profile Creation Wizard Handlers
# =============================================================================
async def handle_start_create_callback(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Start profile creation flow."""
    await callback.answer()
    await state.clear()
    await state.set_state(ProfileCreationSG.waiting_for_name)

    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(MSG_ASK_NAME)


async def handle_creation_name(
    message: Message,
    state: FSMContext,
) -> None:
    """Handle display name input during creation."""
    text_val = message.text or ""
    try:
        clean_name = validate_display_name(text_val)
    except ValueError as err:
        await message.answer(f"❌ {err}\n\nلطفاً نام نمایشی معتبری وارد کنید:")
        return

    await state.update_data(display_name=clean_name)
    await state.set_state(ProfileCreationSG.waiting_for_age)
    await message.answer(MSG_ASK_AGE)


async def handle_creation_age(
    message: Message,
    state: FSMContext,
) -> None:
    """Handle integer age input during creation."""
    text_val = message.text or ""
    try:
        clean_age = validate_age(text_val)
    except ValueError as err:
        await message.answer(f"❌ {err}\n\nلطفاً یک عدد صحیح بین ۱۸ تا ۹۹ وارد کنید:")
        return

    await state.update_data(age=clean_age)
    await state.set_state(ProfileCreationSG.waiting_for_gender)
    await message.answer(
        MSG_ASK_GENDER,
        reply_markup=get_gender_selection_keyboard(is_editing=False),
    )


async def handle_creation_gender(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Handle gender selection via inline button during creation."""
    await callback.answer()
    data = callback.data or ""
    gender_code = data.replace(CB_GENDER_PREFIX, "")

    try:
        clean_gender = validate_gender(gender_code)
    except ValueError as err:
        if isinstance(callback.message, Message):
            await callback.message.answer(f"❌ {err}")
        return

    await state.update_data(gender=clean_gender)
    await state.set_state(ProfileCreationSG.waiting_for_city)

    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            MSG_ASK_CITY,
            reply_markup=get_skip_keyboard(skip_callback=CB_SKIP_CITY),
        )


async def handle_creation_city(
    message: Message,
    state: FSMContext,
) -> None:
    """Handle text input for city."""
    text_val = message.text or ""
    try:
        clean_city = validate_city(text_val)
    except ValueError as err:
        await message.answer(
            f"❌ {err}\n\nلطفاً نام شهر را حداکثر تا ۱۰۰ کاراکتر وارد کنید یا مرحله را رد نمایید:"
        )
        return

    await state.update_data(city=clean_city)
    await state.set_state(ProfileCreationSG.waiting_for_bio)
    await message.answer(
        MSG_ASK_BIO,
        reply_markup=get_skip_keyboard(skip_callback=CB_SKIP_BIO),
    )


async def handle_skip_city_callback(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Handle skipping city step."""
    await callback.answer("شهر رد شد.")
    await state.update_data(city=None)
    await state.set_state(ProfileCreationSG.waiting_for_bio)

    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            MSG_ASK_BIO,
            reply_markup=get_skip_keyboard(skip_callback=CB_SKIP_BIO),
        )


async def handle_creation_bio(
    message: Message,
    state: FSMContext,
) -> None:
    """Handle text input for bio."""
    text_val = message.text or ""
    try:
        clean_bio = validate_bio(text_val)
    except ValueError as err:
        await message.answer(
            f"❌ {err}\n\n"
            "لطفاً متن درباره من را حداکثر تا ۵۰۰ کاراکتر وارد کنید یا مرحله را رد نمایید:"
        )
        return

    await state.update_data(bio=clean_bio)
    await state.set_state(ProfileCreationSG.review_and_confirm)

    wizard_data = await state.get_data()
    await message.answer(
        format_review_summary(wizard_data),
        reply_markup=get_review_keyboard(),
    )


async def handle_skip_bio_callback(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Handle skipping bio step."""
    await callback.answer("درباره من رد شد.")
    await state.update_data(bio=None)
    await state.set_state(ProfileCreationSG.review_and_confirm)

    wizard_data = await state.get_data()
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            format_review_summary(wizard_data),
            reply_markup=get_review_keyboard(),
        )


async def handle_confirm_create_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Persist completed profile to PostgreSQL."""
    await callback.answer("در حال ثبت پروفایل...")

    if callback.from_user is None:
        return

    telegram_user_id = callback.from_user.id
    settings = settings or get_settings()
    db_manager = db_manager or get_database_manager(settings)
    profile_service = ProfileService(db_manager)

    wizard_data = await state.get_data()
    await state.clear()

    # Clear review keyboard to prevent duplicate submissions
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    try:
        profile_data = ProfileData(
            display_name=wizard_data["display_name"],
            age=wizard_data["age"],
            gender=wizard_data["gender"],
            city=wizard_data.get("city"),
            bio=wizard_data.get("bio"),
            visibility="PUBLIC",
        )

        profile = await profile_service.create_profile(telegram_user_id, profile_data)

        if isinstance(callback.message, Message):
            await callback.message.answer(
                format_profile_view(profile, is_new=True),
                reply_markup=get_view_profile_keyboard(),
            )

    except Exception as exc:
        logger.error("Error creating profile for user %s: %s", telegram_user_id, exc)
        if isinstance(callback.message, Message):
            await callback.message.answer(MSG_DATABASE_ERROR)


async def handle_cancel_wizard_callback(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Cancel creation and return to clean state."""
    await callback.answer("عملیات لغو شد.")
    await state.clear()

    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer(
            "عملیات ایجاد پروفایل لغو گردید. هر زمان که مایل بودید می‌توانید مجدداً اقدام کنید.",
            reply_markup=get_main_menu_keyboard(),
        )


# =============================================================================
# Editing Existing Profile Handlers
# =============================================================================
async def handle_edit_field_trigger(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Trigger specific field edit prompt."""
    await callback.answer()
    cb_data = callback.data or ""

    if cb_data == CB_EDIT_NAME:
        await state.set_state(ProfileEditSG.editing_display_name)
        prompt = "لطفاً نام نمایشی جدید خود را وارد نمایید (۲ تا ۵۰ کاراکتر):"
        markup = get_cancel_edit_keyboard()
    elif cb_data == CB_EDIT_AGE:
        await state.set_state(ProfileEditSG.editing_age)
        prompt = "لطفاً سن جدید خود را به عدد وارد فرمایید (۱۸ تا ۹۹ سال):"
        markup = get_cancel_edit_keyboard()
    elif cb_data == CB_EDIT_GENDER:
        await state.set_state(ProfileEditSG.editing_gender)
        prompt = "لطفاً جنسیت جدید خود را انتخاب فرمایید:"
        markup = get_gender_selection_keyboard(is_editing=True)
    elif cb_data == CB_EDIT_CITY:
        await state.set_state(ProfileEditSG.editing_city)
        prompt = "نام شهر جدید خود را وارد نمایید (حداکثر ۱۰۰ کاراکتر):"
        markup = get_cancel_edit_keyboard()
    elif cb_data == CB_EDIT_BIO:
        await state.set_state(ProfileEditSG.editing_bio)
        prompt = "متن جدید درباره من را وارد نمایید (حداکثر ۵۰۰ کاراکتر):"
        markup = get_cancel_edit_keyboard()
    else:
        return

    if isinstance(callback.message, Message):
        await callback.message.answer(prompt, reply_markup=markup)


async def handle_edit_display_name_input(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Save updated display name."""
    if message.from_user is None:
        return

    try:
        clean_name = validate_display_name(message.text or "")
    except ValueError as err:
        await message.answer(
            f"❌ {err}\n\nلطفاً نام نمایشی معتبر وارد کنید یا انصراف دهید:",
            reply_markup=get_cancel_edit_keyboard(),
        )
        return

    telegram_user_id = message.from_user.id
    settings = settings or get_settings()
    db_manager = db_manager or get_database_manager(settings)
    profile_service = ProfileService(db_manager)

    try:
        updated = await profile_service.update_profile_field(
            telegram_user_id, "display_name", clean_name
        )
        await state.clear()
        await message.answer("✅ نام نمایشی با موفقیت تغییر یافت.")
        await message.answer(
            format_profile_view(updated, is_new=False),
            reply_markup=get_view_profile_keyboard(),
        )
    except Exception as exc:
        logger.error("Error updating name for user %s: %s", telegram_user_id, exc)
        await message.answer(MSG_DATABASE_ERROR)


async def handle_edit_age_input(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Save updated age."""
    if message.from_user is None:
        return

    try:
        clean_age = validate_age(message.text or "")
    except ValueError as err:
        await message.answer(
            f"❌ {err}\n\nلطفاً یک عدد صحیح بین ۱۸ تا ۹۹ وارد کنید یا انصراف دهید:",
            reply_markup=get_cancel_edit_keyboard(),
        )
        return

    telegram_user_id = message.from_user.id
    settings = settings or get_settings()
    db_manager = db_manager or get_database_manager(settings)
    profile_service = ProfileService(db_manager)

    try:
        updated = await profile_service.update_profile_field(telegram_user_id, "age", clean_age)
        await state.clear()
        await message.answer("✅ سن شما با موفقیت تغییر یافت.")
        await message.answer(
            format_profile_view(updated, is_new=False),
            reply_markup=get_view_profile_keyboard(),
        )
    except Exception as exc:
        logger.error("Error updating age for user %s: %s", telegram_user_id, exc)
        await message.answer(MSG_DATABASE_ERROR)


async def handle_edit_gender_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Save updated gender from callback."""
    await callback.answer()
    if callback.from_user is None:
        return

    cb_data = callback.data or ""
    gender_code = cb_data.replace(f"{CB_GENDER_PREFIX}edit:", "")

    try:
        clean_gender = validate_gender(gender_code)
    except ValueError as err:
        if isinstance(callback.message, Message):
            await callback.message.answer(f"❌ {err}")
        return

    telegram_user_id = callback.from_user.id
    settings = settings or get_settings()
    db_manager = db_manager or get_database_manager(settings)
    profile_service = ProfileService(db_manager)

    try:
        updated = await profile_service.update_profile_field(
            telegram_user_id, "gender", clean_gender
        )
        await state.clear()
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer("✅ جنسیت شما با موفقیت به‌روزرسانی شد.")
            await callback.message.answer(
                format_profile_view(updated, is_new=False),
                reply_markup=get_view_profile_keyboard(),
            )
    except Exception as exc:
        logger.error("Error updating gender for user %s: %s", telegram_user_id, exc)
        if isinstance(callback.message, Message):
            await callback.message.answer(MSG_DATABASE_ERROR)


async def handle_edit_city_input(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Save updated city."""
    if message.from_user is None:
        return

    try:
        clean_city = validate_city(message.text or "")
    except ValueError as err:
        await message.answer(
            f"❌ {err}\n\nلطفاً نام شهر را حداکثر تا ۱۰۰ کاراکتر وارد کنید:",
            reply_markup=get_cancel_edit_keyboard(),
        )
        return

    telegram_user_id = message.from_user.id
    settings = settings or get_settings()
    db_manager = db_manager or get_database_manager(settings)
    profile_service = ProfileService(db_manager)

    try:
        updated = await profile_service.update_profile_field(telegram_user_id, "city", clean_city)
        await state.clear()
        await message.answer("✅ شهر با موفقیت ثبت شد.")
        await message.answer(
            format_profile_view(updated, is_new=False),
            reply_markup=get_view_profile_keyboard(),
        )
    except Exception as exc:
        logger.error("Error updating city for user %s: %s", telegram_user_id, exc)
        await message.answer(MSG_DATABASE_ERROR)


async def handle_edit_bio_input(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Save updated bio."""
    if message.from_user is None:
        return

    try:
        clean_bio = validate_bio(message.text or "")
    except ValueError as err:
        await message.answer(
            f"❌ {err}\n\nلطفاً متن درباره من را حداکثر تا ۵۰۰ کاراکتر وارد کنید:",
            reply_markup=get_cancel_edit_keyboard(),
        )
        return

    telegram_user_id = message.from_user.id
    settings = settings or get_settings()
    db_manager = db_manager or get_database_manager(settings)
    profile_service = ProfileService(db_manager)

    try:
        updated = await profile_service.update_profile_field(telegram_user_id, "bio", clean_bio)
        await state.clear()
        await message.answer("✅ بخش «درباره من» با موفقیت به‌روزرسانی شد.")
        await message.answer(
            format_profile_view(updated, is_new=False),
            reply_markup=get_view_profile_keyboard(),
        )
    except Exception as exc:
        logger.error("Error updating bio for user %s: %s", telegram_user_id, exc)
        await message.answer(MSG_DATABASE_ERROR)


async def handle_cancel_edit_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Cancel field edit and return to profile view without modifications."""
    await callback.answer("ویرایش لغو شد.")
    await state.clear()

    if callback.from_user is None:
        return

    telegram_user_id = callback.from_user.id
    settings = settings or get_settings()
    db_manager = db_manager or get_database_manager(settings)
    profile_service = ProfileService(db_manager)

    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        try:
            profile = await profile_service.get_profile_by_telegram_id(telegram_user_id)
            if profile is not None:
                await callback.message.answer(
                    format_profile_view(profile, is_new=False),
                    reply_markup=get_view_profile_keyboard(),
                )
        except Exception as exc:
            logger.error(
                "Error displaying profile after cancel for user %s: %s", telegram_user_id, exc
            )
            await callback.message.answer(MSG_DATABASE_ERROR)


def get_profile_router() -> Router:
    """Create and configure the user profile router."""
    router = Router(name="profile")

    # Entry point
    router.message.register(handle_profile_menu_entry, F.text == BTN_MY_PROFILE)

    # Creation wizard
    router.callback_query.register(handle_start_create_callback, F.data == CB_START_CREATE)
    router.message.register(handle_creation_name, ProfileCreationSG.waiting_for_name)
    router.message.register(handle_creation_age, ProfileCreationSG.waiting_for_age)
    router.callback_query.register(
        handle_creation_gender,
        ProfileCreationSG.waiting_for_gender,
        F.data.startswith(CB_GENDER_PREFIX) & ~F.data.startswith(f"{CB_GENDER_PREFIX}edit:"),
    )
    router.callback_query.register(
        handle_skip_city_callback, ProfileCreationSG.waiting_for_city, F.data == CB_SKIP_CITY
    )
    router.message.register(handle_creation_city, ProfileCreationSG.waiting_for_city)
    router.callback_query.register(
        handle_skip_bio_callback, ProfileCreationSG.waiting_for_bio, F.data == CB_SKIP_BIO
    )
    router.message.register(handle_creation_bio, ProfileCreationSG.waiting_for_bio)
    router.callback_query.register(
        handle_confirm_create_callback,
        ProfileCreationSG.review_and_confirm,
        F.data == CB_CONFIRM_CREATE,
    )
    router.callback_query.register(handle_cancel_wizard_callback, F.data == CB_CANCEL_WIZARD)

    # Editing callbacks and inputs
    router.callback_query.register(
        handle_edit_field_trigger,
        F.data.in_({CB_EDIT_NAME, CB_EDIT_AGE, CB_EDIT_GENDER, CB_EDIT_CITY, CB_EDIT_BIO}),
    )
    router.callback_query.register(handle_cancel_edit_callback, F.data == CB_BACK_TO_PROFILE)
    router.message.register(handle_edit_display_name_input, ProfileEditSG.editing_display_name)
    router.message.register(handle_edit_age_input, ProfileEditSG.editing_age)
    router.callback_query.register(
        handle_edit_gender_callback,
        ProfileEditSG.editing_gender,
        F.data.startswith(f"{CB_GENDER_PREFIX}edit:"),
    )
    router.message.register(handle_edit_city_input, ProfileEditSG.editing_city)
    router.message.register(handle_edit_bio_input, ProfileEditSG.editing_bio)

    return router
