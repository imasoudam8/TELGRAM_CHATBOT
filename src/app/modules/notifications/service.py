"""Bot notification helper for peer chat events, direct requests, and match alerts."""

import asyncio
import html
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)

from src.app.bot.keyboards.profile import GENDER_LABELS
from src.app.modules.discovery.schemas import PublicProfileDTO, format_relative_activity

logger = logging.getLogger(__name__)

MAX_TELEGRAM_RETRY_AFTER_SECONDS = 5.0


async def execute_telegram_send_with_retry(
    operation: Callable[[], Awaitable[Any]],
    *,
    context_label: str,
) -> bool:
    """Execute an outgoing Telegram API send with bounded Retry-After handling.

    - Retries once when Telegram returns TelegramRetryAfter <= 5 seconds.
    - Does not retry permanent errors (e.g., TelegramForbiddenError / TelegramBadRequest).
    - Retries once after a brief backoff on transient TelegramNetworkError.
    - Never logs message bodies, file IDs, or bot tokens.
    """
    for attempt in range(2):
        try:
            await operation()
            return True
        except TelegramRetryAfter as exc:
            delay = float(getattr(exc, "retry_after", 0) or 0)
            if attempt == 0 and 0 < delay <= MAX_TELEGRAM_RETRY_AFTER_SECONDS:
                logger.warning(
                    "Telegram rate limit (%s); retrying after %.2fs",
                    context_label,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            logger.warning(
                "Telegram rate limit (%s) exceeded retry ceiling (retry_after=%s)",
                context_label,
                delay,
            )
            return False
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            logger.warning(
                "Telegram rejected delivery (%s: %s)",
                context_label,
                type(exc).__name__,
            )
            return False
        except TelegramNetworkError as exc:
            if attempt == 0:
                logger.warning(
                    "Transient Telegram network error (%s: %s); retrying once",
                    context_label,
                    type(exc).__name__,
                )
                await asyncio.sleep(0.25)
                continue
            logger.warning(
                "Telegram network delivery failed (%s: %s)",
                context_label,
                type(exc).__name__,
            )
            return False
        except Exception as exc:
            logger.warning(
                "Telegram delivery failed (%s: %s)",
                context_label,
                type(exc).__name__,
            )
            return False
    return False


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
        """Send a Telegram message cleanly, handling rate limits and failures without raising."""
        if bot is None or telegram_user_id <= 0:
            return False
        return await execute_telegram_send_with_retry(
            lambda: bot.send_message(
                chat_id=telegram_user_id,
                text=text,
                reply_markup=reply_markup,
            ),
            context_label="notification",
        )
