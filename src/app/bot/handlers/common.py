"""Shared helper utilities for Telegram bot handlers."""

import html
import logging
from typing import Any

from aiogram import Bot
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.app.bot.keyboards.chat import (
    get_active_chat_inline_keyboard,
    get_active_chat_reply_keyboard,
)
from src.app.bot.keyboards.onboarding import (
    get_age_declaration_keyboard,
    get_main_menu_keyboard,
)
from src.app.bot.keyboards.profile import GENDER_LABELS
from src.app.config.settings import Settings
from src.app.infrastructure.database.session import DatabaseManager
from src.app.infrastructure.redis.client import RedisManager
from src.app.modules.discovery.schemas import PublicProfileDTO, UserPreferencesDTO
from src.app.modules.moderation.enforcement import (
    evaluate_user_restrictions_in_session,
    format_account_restriction_message,
)
from src.app.modules.notifications.service import NotificationService, format_public_profile_card
from src.app.modules.users.models import User

logger = logging.getLogger(__name__)

MSG_UNREGISTERED = (
    "⚠️ برای استفاده از این بخش، ابتدا باید شرایط سنی ۱۸+ سال را تأیید نمایید.\n\n"
    "لطفاً از طریق دکمه زیر وضعیت سنی خود را مشخص فرمایید:"
)
MSG_ACCOUNT_RESTRICTED = (
    "🚫 حساب کاربری شما در حال حاضر غیرفعال یا محدود شده است و امکان استفاده از این بخش وجود ندارد."
)
MSG_PROFILE_REQUIRED = (
    "👤 برای ارسال درخواست یا شروع گفتگو، ابتدا باید پروفایل خود را تکمیل کنید.\n\n"
    "لطفاً از منوی اصلی گزینه «👤 پروفایل من» را انتخاب نمایید."
)
MSG_GENERIC_ERROR = (
    "متأسفانه در پردازش درخواست شما خطایی رخ داده است. لطفاً لحظاتی بعد مجدداً تلاش نمایید."
)


def resolve_redis_manager(redis_manager: RedisManager | None) -> RedisManager | None:
    """Return redis_manager only if initialized and safe to invoke."""
    if redis_manager is not None and getattr(redis_manager, "_client", None) is not None:
        return redis_manager
    return None


async def load_authorized_user(
    db_manager: DatabaseManager,
    telegram_user_id: int,
    *,
    require_completed_profile: bool = False,
    allow_chat_restricted: bool = True,
    settings: Settings | None = None,
) -> tuple[User | None, str | None]:
    """Load user with profile and verify adult declaration, ACTIVE status, and restrictions.

    Returns (user, localized_error_message).
    """
    async with db_manager.session() as session:
        res = await session.execute(
            select(User)
            .options(selectinload(User.profile))
            .where(User.telegram_user_id == telegram_user_id)
            .where(User.status != "DELETED")
        )
        user = res.scalar_one_or_none()
        if user is None or not user.age_18_plus_declared:
            return None, MSG_UNREGISTERED

        restriction = await evaluate_user_restrictions_in_session(session, user)

    if restriction.account_status != "ACTIVE":
        return None, format_account_restriction_message(restriction, settings=settings)
    if not allow_chat_restricted and restriction.is_chat_restricted:
        return None, format_account_restriction_message(restriction, settings=settings)
    if require_completed_profile and (
        user.profile is None or user.profile.completion_status != "COMPLETED"
    ):
        return None, MSG_PROFILE_REQUIRED
    return user, None


async def reply_with_auth_error(
    target: Message | CallbackQuery,
    error_text: str,
) -> None:
    """Send localized authorization/profile requirement feedback appropriately."""
    msg = target if isinstance(target, Message) else target.message
    if not isinstance(msg, Message):
        return
    if error_text == MSG_UNREGISTERED:
        await msg.answer(error_text, reply_markup=get_age_declaration_keyboard())
    else:
        await msg.answer(error_text, reply_markup=get_main_menu_keyboard())


def format_preferences_summary(prefs: UserPreferencesDTO) -> str:
    """Render user's matching & discovery filters in Persian HTML."""
    if prefs.preferred_genders:
        genders_str = "، ".join(GENDER_LABELS.get(g, g) for g in prefs.preferred_genders)
    else:
        genders_str = "همه"

    age_str = f"{prefs.min_age} تا {prefs.max_age} سال"
    cities_str = (
        "، ".join(html.escape(c) for c in prefs.preferred_cities)
        if prefs.preferred_cities
        else "همه شهرها"
    )

    if prefs.recent_activity_hours is None:
        activity_str = "همه زمان‌ها"
    elif prefs.recent_activity_hours <= 24:
        activity_str = "۲۴ ساعت اخیر"
    elif prefs.recent_activity_hours <= 168:
        activity_str = "۷ روز اخیر"
    else:
        activity_str = f"{prefs.recent_activity_hours} ساعت اخیر"

    relax_names = {
        "gender": "جنسیت",
        "age": "سن",
        "city": "شهر",
        "recent_activity": "فعالیت اخیر",
    }
    if prefs.relaxable_fields:
        relax_str = "، ".join(relax_names[f] for f in sorted(prefs.relaxable_fields))
    else:
        relax_str = "هیچکدام (همه فیلترها قطعی هستند)"

    return (
        "🎛 <b>تنظیمات فیلترهای جستجو و گفتگوی تصادفی</b>\n\n"
        f"⚧ <b>جنسیت مورد نظر:</b> {genders_str}\n"
        f"🎂 <b>بازه سنی:</b> {age_str}\n"
        f"🏙 <b>شهرها:</b> {cities_str}\n"
        f"🕒 <b>فعالیت اخیر:</b> {activity_str}\n"
        f"🔓 <b>فیلترهای قابل انعطاف:</b> {relax_str}\n\n"
        "<i>فیلترهای غیرقابل انعطاف (🔒) هرگز به صورت خودکار نادیده گرفته نمی‌شوند.</i>"
    )


async def notify_chat_started(
    bot: Bot | None,
    *,
    chat_id: Any,
    user1_telegram_id: int,
    user1_sees_profile: PublicProfileDTO,
    user2_telegram_id: int,
    user2_sees_profile: PublicProfileDTO,
    intro_title: str = "💬 <b>گفتگوی ناشناس آغاز شد!</b>",
) -> None:
    """Notify both chat participants cleanly without exposing Telegram IDs."""
    notifier = NotificationService()
    inline_kb = get_active_chat_inline_keyboard(chat_id)
    reply_kb = get_active_chat_reply_keyboard()

    for tg_id, peer_profile in (
        (user1_telegram_id, user1_sees_profile),
        (user2_telegram_id, user2_sees_profile),
    ):
        card = format_public_profile_card(peer_profile, header=intro_title)
        await notifier.send_safe_message(bot, tg_id, card, reply_markup=inline_kb)
        await notifier.send_safe_message(
            bot,
            tg_id,
            "✍️ هم‌اکنون می‌توانید پیام خود را ارسال کنید. "
            "برای پایان گفتگو از دکمه زیر استفاده نمایید:",
            reply_markup=reply_kb,
        )
