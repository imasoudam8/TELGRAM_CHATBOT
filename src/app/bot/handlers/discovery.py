"""Telegram handlers for profile discovery, public ID lookup, and preference editing."""

import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.app.bot.handlers.common import (
    MSG_GENERIC_ERROR,
    format_preferences_summary,
    load_authorized_user,
    reply_with_auth_error,
)
from src.app.bot.keyboards.chat import get_direct_request_decision_keyboard
from src.app.bot.keyboards.discovery import (
    CB_DISC_BLK_PREFIX,
    CB_DISC_BROWSE_PREFIX,
    CB_DISC_LOOKUP,
    CB_DISC_OPEN_PREFIX,
    CB_DISC_PREFS,
    CB_DISC_REQ_PREFIX,
    CB_PREF_ACT_PREFIX,
    CB_PREF_AGE,
    CB_PREF_AGE_CLEAR,
    CB_PREF_CANCEL_INPUT,
    CB_PREF_CITY,
    CB_PREF_CITY_CLEAR,
    CB_PREF_GENDER_CLEAR,
    CB_PREF_GENDER_PREFIX,
    CB_PREF_REL_PREFIX,
    get_cancel_filter_input_keyboard,
    get_discovery_profile_keyboard,
    get_empty_discovery_keyboard,
    get_preferences_keyboard,
    get_public_profile_detail_keyboard,
)
from src.app.bot.keyboards.onboarding import BTN_VIEW_PROFILES, get_main_menu_keyboard
from src.app.bot.states import DiscoveryPrefsSG
from src.app.config.settings import Settings, get_settings
from src.app.infrastructure.database.session import DatabaseManager, get_database_manager
from src.app.modules.chat.connections import ConnectionAndBlockService
from src.app.modules.chat.direct_requests import DirectRequestService
from src.app.modules.discovery.schemas import ALLOWED_ACTIVITY_WINDOWS_HOURS, RELAXABLE_FIELD_KEYS
from src.app.modules.discovery.service import DiscoveryService
from src.app.modules.notifications.service import NotificationService, format_public_profile_card

logger = logging.getLogger(__name__)


async def _render_discovery_page(
    target: Message,
    db_manager: DatabaseManager,
    viewer_user_id: int,
    *,
    offset: int = 0,
    use_relaxed: bool = False,
) -> None:
    discovery_service = DiscoveryService(db_manager)
    page = await discovery_service.browse_profiles(
        viewer_user_id,
        offset=offset,
        limit=1,
        use_relaxed=use_relaxed,
    )

    if not page.items:
        if page.relaxed_available:
            text = (
                "🔎 <b>نتیجه‌ای با فیلترهای قطعی فعلی یافت نشد.</b>\n\n"
                f"💡 با توجه به فیلترهای قابل انعطاف انتخابی شما، "
                f"<b>{page.relaxed_count}</b> پروفایل پیشنهادی دیگر در دسترس است.\n"
                "در صورت تمایل می‌توانید آن‌ها را مشاهده کنید یا فیلترهای خود را تغییر دهید:"
            )
        else:
            text = (
                "🔎 <b>در حال حاضر پروفایل جدیدی مطابق فیلترهای شما یافت نشد.</b>\n\n"
                "می‌توانید فیلترهای جستجو را تنظیم کنید یا با شناسه عمومی جستجو نمایید:"
            )
        await target.answer(text, reply_markup=get_empty_discovery_keyboard(page))
        return

    profile = page.items[0]
    mode_tag = " (پیشنهاد با فیلتر قابل انعطاف)" if page.used_relaxed else ""
    header = f"🔎 <b>پروفایل {page.offset + 1} از {page.total_count}</b>{mode_tag}"
    await target.answer(
        format_public_profile_card(profile, header=header),
        reply_markup=get_discovery_profile_keyboard(profile, page),
    )


async def handle_view_profiles_entry(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Handle '🔎 مشاهده پروفایلها' main menu entry."""
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

        await _render_discovery_page(message, resolved_db, user.id, offset=0, use_relaxed=False)
    except Exception as exc:
        logger.error("Error browsing profiles: %s", type(exc).__name__)
        await message.answer(MSG_GENERIC_ERROR)


async def handle_discovery_browse_callback(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Handle pagination or relaxed-browse callback."""
    await callback.answer()
    if callback.from_user is None or not isinstance(callback.message, Message):
        return

    raw = (callback.data or "").replace(CB_DISC_BROWSE_PREFIX, "")
    parts = raw.split(":")
    try:
        offset = max(0, int(parts[0]))
        use_relaxed = parts[1] == "1" if len(parts) > 1 else False
    except (ValueError, IndexError):
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(resolved_db, callback.from_user.id)
        if err is not None or user is None:
            await reply_with_auth_error(callback, err or MSG_GENERIC_ERROR)
            return

        await _render_discovery_page(
            callback.message,
            resolved_db,
            user.id,
            offset=offset,
            use_relaxed=use_relaxed,
        )
    except Exception as exc:
        logger.error("Error during discovery pagination: %s", type(exc).__name__)
        await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_discovery_open_profile_callback(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Open a profile directly by public_id via callback."""
    await callback.answer()
    if callback.from_user is None or not isinstance(callback.message, Message):
        return

    public_id = (callback.data or "").replace(CB_DISC_OPEN_PREFIX, "").strip()
    if not public_id:
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(resolved_db, callback.from_user.id)
        if err is not None or user is None:
            await reply_with_auth_error(callback, err or MSG_GENERIC_ERROR)
            return

        profile = await DiscoveryService(resolved_db).get_public_profile_by_public_id(
            user.id, public_id
        )
        if profile is None:
            await callback.message.answer(
                "❌ پروفایل مورد نظر یافت نشد یا در حال حاضر قابل مشاهده نیست."
            )
            return

        await callback.message.answer(
            format_public_profile_card(profile),
            reply_markup=get_public_profile_detail_keyboard(profile),
        )
    except Exception as exc:
        logger.error("Error opening public profile: %s", type(exc).__name__)
        await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_discovery_lookup_prompt(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Prompt user for a public_id to look up."""
    await callback.answer()
    await state.set_state(DiscoveryPrefsSG.waiting_for_lookup_public_id)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "🆔 لطفاً شناسه عمومی کاربر مورد نظر را وارد کنید "
            "(مثال: <code>usr_abc123def456</code>):",
            reply_markup=get_cancel_filter_input_keyboard(),
        )


async def handle_discovery_lookup_input(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Resolve user-entered public_id and show public profile subject to visibility & blocks."""
    if message.from_user is None:
        return

    target_pid = (message.text or "").strip()
    if not target_pid:
        await message.answer(
            "❌ شناسه عمومی نامعتبر است. لطفاً دوباره وارد کنید:",
            reply_markup=get_cancel_filter_input_keyboard(),
        )
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(resolved_db, message.from_user.id)
        if err is not None or user is None:
            await state.clear()
            await reply_with_auth_error(message, err or MSG_GENERIC_ERROR)
            return

        profile = await DiscoveryService(resolved_db).get_public_profile_by_public_id(
            user.id, target_pid
        )
        await state.clear()
        if profile is None:
            await message.answer(
                "❌ کاربری با این شناسه یافت نشد یا پروفایل او قابل نمایش نیست.",
                reply_markup=get_main_menu_keyboard(),
            )
            return

        await message.answer(
            format_public_profile_card(profile),
            reply_markup=get_public_profile_detail_keyboard(profile),
        )
    except Exception as exc:
        logger.error("Error looking up public ID: %s", type(exc).__name__)
        await state.clear()
        await message.answer(MSG_GENERIC_ERROR)


async def handle_discovery_send_request_callback(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Send a direct chat request directly from a discovered profile card."""
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    target_pid = (callback.data or "").replace(CB_DISC_REQ_PREFIX, "").strip()
    if not target_pid:
        await callback.answer("شناسه نامعتبر است.")
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(
            resolved_db, callback.from_user.id, require_completed_profile=True
        )
        if err is not None or user is None:
            await callback.answer()
            await reply_with_auth_error(callback, err or MSG_GENERIC_ERROR)
            return

        dr_service = DirectRequestService(resolved_db)
        result = await dr_service.send_direct_request(user.id, target_pid)
        await callback.answer("درخواست گفتگو ارسال شد ✅")

        escaped_target_pid = html.escape(result.target_profile.public_id)
        await callback.message.answer(
            f"✅ درخواست گفتگوی مستقیم به کاربر <code>{escaped_target_pid}</code> ارسال شد.\n"
            "⏳ این درخواست به مدت ۲۴ ساعت معتبر است.",
            reply_markup=get_main_menu_keyboard(),
        )

        # Notify recipient with accept/reject inline buttons
        notifier = NotificationService()
        notif_text = format_public_profile_card(
            result.requester_profile,
            header="✉️ <b>درخواست گفتگوی مستقیم جدید!</b>",
        )
        await notifier.send_safe_message(
            callback.bot,
            result.target_telegram_id,
            f"{notif_text}\n\n⏳ این درخواست تا ۲۴ ساعت آینده معتبر است.",
            reply_markup=get_direct_request_decision_keyboard(result.request_id),
        )

    except (ValueError, PermissionError, LookupError, RuntimeError) as domain_err:
        await callback.answer(str(domain_err)[:180], show_alert=True)
    except Exception as exc:
        logger.error("Error sending direct request from discovery: %s", type(exc).__name__)
        await callback.answer()
        await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_discovery_block_callback(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Block a user from discovery profile view."""
    if callback.from_user is None or not isinstance(callback.message, Message):
        await callback.answer()
        return

    target_pid = (callback.data or "").replace(CB_DISC_BLK_PREFIX, "").strip()
    if not target_pid:
        await callback.answer("شناسه نامعتبر است.")
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(resolved_db, callback.from_user.id)
        if err is not None or user is None:
            await callback.answer()
            await reply_with_auth_error(callback, err or MSG_GENERIC_ERROR)
            return

        await ConnectionAndBlockService(resolved_db).block_user(
            user.id, target_pid, reason="DISCOVERY_BLOCK"
        )
        await callback.answer("کاربر مسدود شد 🚫")
        await callback.message.answer(
            f"🚫 کاربر <code>{html.escape(target_pid)}</code> با موفقیت مسدود شد و "
            "دیگر در جستجو و ارتباط‌ها نمایش داده نخواهد شد."
        )
    except (ValueError, PermissionError, LookupError) as domain_err:
        await callback.answer(str(domain_err)[:180], show_alert=True)
    except Exception as exc:
        logger.error("Error blocking user from discovery: %s", type(exc).__name__)
        await callback.answer()
        await callback.message.answer(MSG_GENERIC_ERROR)


# =============================================================================
# Discovery & Matching Preferences Handlers
# =============================================================================
async def handle_preferences_menu_callback(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Display user preferences screen."""
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

        prefs = await DiscoveryService(resolved_db).get_preferences(user.id)
        await callback.message.answer(
            format_preferences_summary(prefs),
            reply_markup=get_preferences_keyboard(prefs),
        )
    except Exception as exc:
        logger.error("Error displaying preferences: %s", type(exc).__name__)
        await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_pref_toggle_gender(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Toggle a gender choice in user preferences."""
    await callback.answer()
    if callback.from_user is None or not isinstance(callback.message, Message):
        return

    gender_code = (callback.data or "").replace(CB_PREF_GENDER_PREFIX, "").strip().upper()
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(resolved_db, callback.from_user.id)
        if err is not None or user is None:
            await reply_with_auth_error(callback, err or MSG_GENERIC_ERROR)
            return

        svc = DiscoveryService(resolved_db)
        prefs = await svc.get_preferences(user.id)
        new_genders = list(prefs.preferred_genders)
        if gender_code in new_genders:
            new_genders.remove(gender_code)
        else:
            new_genders.append(gender_code)

        updated = await svc.save_preferences(
            user.id,
            preferred_genders=new_genders,
            min_age=prefs.min_age,
            max_age=prefs.max_age,
            preferred_cities=prefs.preferred_cities,
            recent_activity_hours=prefs.recent_activity_hours,
            relaxable_fields=prefs.relaxable_fields,
        )
        await callback.message.edit_text(
            format_preferences_summary(updated),
            reply_markup=get_preferences_keyboard(updated),
        )
    except ValueError as err_val:
        await callback.message.answer(f"❌ {err_val}")
    except Exception as exc:
        logger.error("Error updating gender filter: %s", type(exc).__name__)
        await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_pref_clear_gender(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Reset preferred genders to all."""
    await callback.answer("فیلتر جنسیت پاک شد.")
    if callback.from_user is None or not isinstance(callback.message, Message):
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(resolved_db, callback.from_user.id)
        if err is not None or user is None:
            return
        svc = DiscoveryService(resolved_db)
        prefs = await svc.get_preferences(user.id)
        updated = await svc.save_preferences(
            user.id,
            preferred_genders=[],
            min_age=prefs.min_age,
            max_age=prefs.max_age,
            preferred_cities=prefs.preferred_cities,
            recent_activity_hours=prefs.recent_activity_hours,
            relaxable_fields=prefs.relaxable_fields,
        )
        await callback.message.edit_text(
            format_preferences_summary(updated),
            reply_markup=get_preferences_keyboard(updated),
        )
    except Exception as exc:
        logger.error("Error clearing gender filter: %s", type(exc).__name__)


async def handle_pref_activity_select(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Update recent activity preference window."""
    await callback.answer()
    if callback.from_user is None or not isinstance(callback.message, Message):
        return

    raw_hours = (callback.data or "").replace(CB_PREF_ACT_PREFIX, "").strip()
    try:
        hours_val = int(raw_hours)
    except ValueError:
        return

    hours_opt: int | None = None if hours_val == 0 else hours_val
    if hours_opt is not None and hours_opt not in ALLOWED_ACTIVITY_WINDOWS_HOURS:
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(resolved_db, callback.from_user.id)
        if err is not None or user is None:
            return
        svc = DiscoveryService(resolved_db)
        prefs = await svc.get_preferences(user.id)
        updated = await svc.save_preferences(
            user.id,
            preferred_genders=prefs.preferred_genders,
            min_age=prefs.min_age,
            max_age=prefs.max_age,
            preferred_cities=prefs.preferred_cities,
            recent_activity_hours=hours_opt,
            relaxable_fields=prefs.relaxable_fields,
        )
        await callback.message.edit_text(
            format_preferences_summary(updated),
            reply_markup=get_preferences_keyboard(updated),
        )
    except Exception as exc:
        logger.error("Error updating activity filter: %s", type(exc).__name__)


async def handle_pref_toggle_relaxable(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Toggle whether a filter dimension is explicitly permitted to relax."""
    await callback.answer()
    if callback.from_user is None or not isinstance(callback.message, Message):
        return

    field = (callback.data or "").replace(CB_PREF_REL_PREFIX, "").strip()
    if field not in RELAXABLE_FIELD_KEYS:
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(resolved_db, callback.from_user.id)
        if err is not None or user is None:
            return
        svc = DiscoveryService(resolved_db)
        prefs = await svc.get_preferences(user.id)
        new_rel = set(prefs.relaxable_fields)
        if field in new_rel:
            new_rel.remove(field)
        else:
            new_rel.add(field)

        updated = await svc.save_preferences(
            user.id,
            preferred_genders=prefs.preferred_genders,
            min_age=prefs.min_age,
            max_age=prefs.max_age,
            preferred_cities=prefs.preferred_cities,
            recent_activity_hours=prefs.recent_activity_hours,
            relaxable_fields=new_rel,
        )
        await callback.message.edit_text(
            format_preferences_summary(updated),
            reply_markup=get_preferences_keyboard(updated),
        )
    except Exception as exc:
        logger.error("Error toggling relaxable field: %s", type(exc).__name__)


async def handle_pref_age_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    """Prompt for min-max age filter input."""
    await callback.answer()
    await state.set_state(DiscoveryPrefsSG.waiting_for_age_range)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "🎂 لطفاً بازه سنی مورد نظر خود را به صورت "
            "<code>حداقل-حداکثر</code> وارد کنید (مثال: <code>22-35</code>):",
            reply_markup=get_cancel_filter_input_keyboard(),
        )


async def handle_pref_age_clear(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Reset age range filter to 18-99."""
    await callback.answer("بازه سنی بازنشانی شد.")
    if callback.from_user is None or not isinstance(callback.message, Message):
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, _ = await load_authorized_user(resolved_db, callback.from_user.id)
        if user is None:
            return
        svc = DiscoveryService(resolved_db)
        prefs = await svc.get_preferences(user.id)
        updated = await svc.save_preferences(
            user.id,
            preferred_genders=prefs.preferred_genders,
            min_age=18,
            max_age=99,
            preferred_cities=prefs.preferred_cities,
            recent_activity_hours=prefs.recent_activity_hours,
            relaxable_fields=prefs.relaxable_fields,
        )
        await callback.message.edit_text(
            format_preferences_summary(updated),
            reply_markup=get_preferences_keyboard(updated),
        )
    except Exception as exc:
        logger.error("Error resetting age filter: %s", type(exc).__name__)


async def handle_pref_age_input(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Parse and save user-entered age range filter."""
    if message.from_user is None:
        return

    raw = (message.text or "").strip().replace(" ", "-")
    parts = [p for p in raw.split("-") if p]
    if len(parts) != 2:
        await message.answer(
            "❌ فرمت وارد شده صحیح نیست. "
            "لطفاً دو عدد بین ۱۸ تا ۹۹ با خط تیره وارد کنید (مثال: 20-35):",
            reply_markup=get_cancel_filter_input_keyboard(),
        )
        return

    try:
        min_a, max_a = int(parts[0]), int(parts[1])
    except ValueError:
        await message.answer(
            "❌ مقادیر باید عدد صحیح باشند (مثال: 20-35):",
            reply_markup=get_cancel_filter_input_keyboard(),
        )
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(resolved_db, message.from_user.id)
        if err is not None or user is None:
            await state.clear()
            await reply_with_auth_error(message, err or MSG_GENERIC_ERROR)
            return

        svc = DiscoveryService(resolved_db)
        prefs = await svc.get_preferences(user.id)
        updated = await svc.save_preferences(
            user.id,
            preferred_genders=prefs.preferred_genders,
            min_age=min_a,
            max_age=max_a,
            preferred_cities=prefs.preferred_cities,
            recent_activity_hours=prefs.recent_activity_hours,
            relaxable_fields=prefs.relaxable_fields,
        )
        await state.clear()
        await message.answer("✅ بازه سنی با موفقیت ذخیره شد.")
        await message.answer(
            format_preferences_summary(updated),
            reply_markup=get_preferences_keyboard(updated),
        )
    except ValueError as err_val:
        await message.answer(
            f"❌ {err_val}",
            reply_markup=get_cancel_filter_input_keyboard(),
        )
    except Exception as exc:
        logger.error("Error saving age range filter: %s", type(exc).__name__)
        await state.clear()
        await message.answer(MSG_GENERIC_ERROR)


async def handle_pref_city_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    """Prompt for preferred cities list."""
    await callback.answer()
    await state.set_state(DiscoveryPrefsSG.waiting_for_cities)
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "🏙 لطفاً نام شهر یا شهرهای مورد نظر خود را وارد کنید "
            "(در صورت چند شهر با ویرگول جدا کنید، مثال: <code>تهران، شیراز</code>):",
            reply_markup=get_cancel_filter_input_keyboard(),
        )


async def handle_pref_city_clear(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Clear preferred city filter."""
    await callback.answer("فیلتر شهر پاک شد.")
    if callback.from_user is None or not isinstance(callback.message, Message):
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, _ = await load_authorized_user(resolved_db, callback.from_user.id)
        if user is None:
            return
        svc = DiscoveryService(resolved_db)
        prefs = await svc.get_preferences(user.id)
        updated = await svc.save_preferences(
            user.id,
            preferred_genders=prefs.preferred_genders,
            min_age=prefs.min_age,
            max_age=prefs.max_age,
            preferred_cities=[],
            recent_activity_hours=prefs.recent_activity_hours,
            relaxable_fields=prefs.relaxable_fields,
        )
        await callback.message.edit_text(
            format_preferences_summary(updated),
            reply_markup=get_preferences_keyboard(updated),
        )
    except Exception as exc:
        logger.error("Error clearing city filter: %s", type(exc).__name__)


async def handle_pref_city_input(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Parse and save user-entered cities list."""
    if message.from_user is None:
        return

    raw = (message.text or "").replace("،", ",")
    cities = [part.strip() for part in raw.split(",") if part.strip()]
    if not cities:
        await message.answer(
            "❌ حداقل یک نام شهر معتبر وارد کنید یا انصراف دهید:",
            reply_markup=get_cancel_filter_input_keyboard(),
        )
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(resolved_db, message.from_user.id)
        if err is not None or user is None:
            await state.clear()
            await reply_with_auth_error(message, err or MSG_GENERIC_ERROR)
            return

        svc = DiscoveryService(resolved_db)
        prefs = await svc.get_preferences(user.id)
        updated = await svc.save_preferences(
            user.id,
            preferred_genders=prefs.preferred_genders,
            min_age=prefs.min_age,
            max_age=prefs.max_age,
            preferred_cities=cities,
            recent_activity_hours=prefs.recent_activity_hours,
            relaxable_fields=prefs.relaxable_fields,
        )
        await state.clear()
        await message.answer("✅ فیلتر شهرها با موفقیت ذخیره شد.")
        await message.answer(
            format_preferences_summary(updated),
            reply_markup=get_preferences_keyboard(updated),
        )
    except ValueError as err_val:
        await message.answer(
            f"❌ {err_val}",
            reply_markup=get_cancel_filter_input_keyboard(),
        )
    except Exception as exc:
        logger.error("Error saving city filter: %s", type(exc).__name__)
        await state.clear()
        await message.answer(MSG_GENERIC_ERROR)


async def handle_pref_cancel_input(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    """Cancel FSM input in discovery/preferences."""
    await callback.answer("انصراف انجام شد.")
    await state.clear()
    if isinstance(callback.message, Message):
        await callback.message.answer(
            "عملیات لغو شد.",
            reply_markup=get_main_menu_keyboard(),
        )


def get_discovery_router() -> Router:
    """Create and configure the discovery & preferences router."""
    router = Router(name="discovery")

    router.message.register(handle_view_profiles_entry, F.text == BTN_VIEW_PROFILES)
    router.callback_query.register(
        handle_discovery_browse_callback, F.data.startswith(CB_DISC_BROWSE_PREFIX)
    )
    router.callback_query.register(
        handle_discovery_open_profile_callback, F.data.startswith(CB_DISC_OPEN_PREFIX)
    )
    router.callback_query.register(handle_discovery_lookup_prompt, F.data == CB_DISC_LOOKUP)
    router.message.register(
        handle_discovery_lookup_input, DiscoveryPrefsSG.waiting_for_lookup_public_id
    )
    router.callback_query.register(
        handle_discovery_send_request_callback, F.data.startswith(CB_DISC_REQ_PREFIX)
    )
    router.callback_query.register(
        handle_discovery_block_callback, F.data.startswith(CB_DISC_BLK_PREFIX)
    )

    router.callback_query.register(handle_preferences_menu_callback, F.data == CB_DISC_PREFS)
    router.callback_query.register(
        handle_pref_toggle_gender, F.data.startswith(CB_PREF_GENDER_PREFIX)
    )
    router.callback_query.register(handle_pref_clear_gender, F.data == CB_PREF_GENDER_CLEAR)
    router.callback_query.register(
        handle_pref_activity_select, F.data.startswith(CB_PREF_ACT_PREFIX)
    )
    router.callback_query.register(
        handle_pref_toggle_relaxable, F.data.startswith(CB_PREF_REL_PREFIX)
    )
    router.callback_query.register(handle_pref_age_prompt, F.data == CB_PREF_AGE)
    router.callback_query.register(handle_pref_age_clear, F.data == CB_PREF_AGE_CLEAR)
    router.message.register(handle_pref_age_input, DiscoveryPrefsSG.waiting_for_age_range)
    router.callback_query.register(handle_pref_city_prompt, F.data == CB_PREF_CITY)
    router.callback_query.register(handle_pref_city_clear, F.data == CB_PREF_CITY_CLEAR)
    router.message.register(handle_pref_city_input, DiscoveryPrefsSG.waiting_for_cities)
    router.callback_query.register(handle_pref_cancel_input, F.data == CB_PREF_CANCEL_INPUT)

    return router
