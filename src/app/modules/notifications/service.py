"""Bot notification helper for peer chat events, direct requests, and match alerts."""

import html
import logging
from typing import Any

from aiogram import Bot

from src.app.bot.keyboards.profile import GENDER_LABELS
from src.app.modules.discovery.schemas import PublicProfileDTO, format_relative_activity

logger = logging.getLogger(__name__)


def format_public_profile_card(
    profile: PublicProfileDTO,
    *,
    header: str = "👤 <b>پروفایل کاربر</b>",
) -> str:
    """Format a PublicProfileDTO safely in Persian HTML without exposing Telegram IDs."""
    escaped_pid = html.escape(profile.public_id)
    escaped_name = html.escape(profile.display_name)
    gender_label = GENDER_LABELS.get(profile.gender, html.escape(profile.gender))
    city_text = html.escape(profile.city) if profile.city else "<i>ثبت نشده</i>"
    bio_text = html.escape(profile.bio) if profile.bio else "<i>ثبت نشده</i>"
    activity_text = format_relative_activity(profile.last_activity_at)

    status_note = (
        "\n⚠️ <i>این کاربر در حال حاضر در گفتگوی دیگری مشغول است.</i>"
        if profile.in_active_chat
        else ""
    )

    return (
        f"{header}\n\n"
        f"🆔 <b>شناسه عمومی:</b> <code>{escaped_pid}</code>\n"
        f"🏷 <b>نام نمایشی:</b> {escaped_name}\n"
        f"🎂 <b>سن:</b> {profile.age} سال\n"
        f"⚧ <b>جنسیت:</b> {gender_label}\n"
        f"🏙 <b>شهر:</b> {city_text}\n"
        f"✍️ <b>درباره من:</b>\n{bio_text}\n\n"
        f"🕒 <b>آخرین فعالیت:</b> {activity_text}"
        f"{status_note}"
    )


class NotificationService:
    """Delivers privacy-safe Telegram notifications without failing core domain transactions."""

    async def send_safe_message(
        self,
        bot: Bot | None,
        telegram_user_id: int,
        text: str,
        *,
        reply_markup: Any | None = None,
    ) -> bool:
        """Send a Telegram message cleanly, logging failures without raising."""
        if bot is None or telegram_user_id <= 0:
            return False
        try:
            await bot.send_message(
                chat_id=telegram_user_id,
                text=text,
                reply_markup=reply_markup,
            )
            return True
        except Exception as exc:
            logger.warning(
                "Failed to deliver Telegram notification (%s)",
                type(exc).__name__,
            )
            return False
