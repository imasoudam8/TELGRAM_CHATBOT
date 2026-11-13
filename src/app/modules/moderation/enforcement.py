"""Shared account restriction, suspension expiration, and chat restriction enforcement helpers."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.config.settings import Settings
from src.app.infrastructure.database.base import now_utc, to_business_tz
from src.app.modules.moderation.models import ModerationAction

if TYPE_CHECKING:
    from src.app.modules.users.models import User


def as_utc(dt: datetime) -> datetime:
    """Normalize naive or tz-aware datetime to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


@dataclass(frozen=True)
class AccountRestrictionStatus:
    """Evaluated account restriction status for a user without exposing sensitive IDs."""

    user_id: int
    account_status: str
    is_chat_restricted: bool
    restriction_reason: str | None = None
    expires_at: datetime | None = None


async def reconcile_user_suspension_in_session(
    session: AsyncSession,
    user: User | None,
    *,
    utc_now: datetime | None = None,
) -> bool:
    """Automatically expire an elapsed TEMPORARY_SUSPENSION without needing a cron job.

    Returns True if an expired suspension was cleared and persisted in the session.
    Permanent BANNED or DELETED accounts are never modified by automatic expiration.
    """
    if user is None or user.status != "SUSPENDED" or user.suspended_until is None:
        return False

    ts = as_utc(utc_now) if utc_now is not None else now_utc()
    if as_utc(user.suspended_until) <= ts:
        user.status = "ACTIVE"
        user.suspended_until = None
        user.suspension_reason = None
        await session.flush()
        return True
    return False


async def get_active_chat_restriction_in_session(
    session: AsyncSession,
    user_id: int,
    *,
    utc_now: datetime | None = None,
) -> ModerationAction | None:
    """Return the latest active (non-expired) CHAT_RESTRICTION action for user_id, if any."""
    ts = as_utc(utc_now) if utc_now is not None else now_utc()
    stmt = (
        select(ModerationAction)
        .where(
            ModerationAction.user_id == user_id,
            ModerationAction.action_type == "CHAT_RESTRICTION",
            (ModerationAction.expires_at.is_(None) | (ModerationAction.expires_at > ts)),
        )
        .order_by(ModerationAction.created_at.desc(), ModerationAction.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def has_active_chat_restriction_in_session(
    session: AsyncSession,
    user_id: int,
    *,
    utc_now: datetime | None = None,
) -> bool:
    """Return True if user_id currently has an active CHAT_RESTRICTION."""
    action = await get_active_chat_restriction_in_session(session, user_id, utc_now=utc_now)
    return action is not None


async def evaluate_user_restrictions_in_session(
    session: AsyncSession,
    user: User,
    *,
    utc_now: datetime | None = None,
) -> AccountRestrictionStatus:
    """Reconcile expired suspensions and evaluate current account & chat restrictions."""
    ts = as_utc(utc_now) if utc_now is not None else now_utc()
    await reconcile_user_suspension_in_session(session, user, utc_now=ts)

    if user.status == "SUSPENDED":
        return AccountRestrictionStatus(
            user_id=user.id,
            account_status="SUSPENDED",
            is_chat_restricted=True,
            restriction_reason=user.suspension_reason,
            expires_at=as_utc(user.suspended_until) if user.suspended_until else None,
        )

    if user.status == "BANNED":
        return AccountRestrictionStatus(
            user_id=user.id,
            account_status="BANNED",
            is_chat_restricted=True,
            restriction_reason=user.suspension_reason,
            expires_at=None,
        )

    if user.status == "DELETED":
        return AccountRestrictionStatus(
            user_id=user.id,
            account_status="DELETED",
            is_chat_restricted=True,
            restriction_reason=None,
            expires_at=None,
        )

    chat_restr = await get_active_chat_restriction_in_session(session, user.id, utc_now=ts)
    return AccountRestrictionStatus(
        user_id=user.id,
        account_status=user.status,
        is_chat_restricted=chat_restr is not None,
        restriction_reason=chat_restr.reason if chat_restr is not None else None,
        expires_at=as_utc(chat_restr.expires_at)
        if chat_restr is not None and chat_restr.expires_at is not None
        else None,
    )


def format_account_restriction_message(
    status: AccountRestrictionStatus,
    *,
    settings: Settings | None = None,
) -> str:
    """Build a privacy-safe Persian HTML message explaining an active restriction."""
    if status.account_status == "BANNED":
        lines = ["🚫 <b>حساب کاربری شما به صورت دائم مسدود شده است.</b>"]
        if status.restriction_reason:
            lines.append(f"📌 <b>دلیل:</b> {html.escape(status.restriction_reason)}")
        lines.append("امکان استفاده از بخش‌های ربات برای این حساب وجود ندارد.")
        return "\n\n".join(lines)

    if status.account_status == "SUSPENDED":
        lines = ["⛔ <b>حساب کاربری شما به صورت موقت تعلیق شده است.</b>"]
        if status.restriction_reason:
            lines.append(f"📌 <b>دلیل:</b> {html.escape(status.restriction_reason)}")
        if status.expires_at is not None and settings is not None:
            local_exp = to_business_tz(status.expires_at, settings.business_timezone)
            lines.append(
                f"⏳ <b>زمان پایان تعلیق:</b> {local_exp.strftime('%Y-%m-%d %H:%M')} (به وقت تهران)"
            )
        elif status.expires_at is not None:
            lines.append(
                f"⏳ <b>زمان پایان تعلیق (UTC):</b> {status.expires_at.strftime('%Y-%m-%d %H:%M')}"
            )
        lines.append("پس از پایان مهلت یا بازبینی پشتیبانی، دسترسی شما باز خواهد گشت.")
        return "\n\n".join(lines)

    if status.is_chat_restricted:
        lines = ["🔇 <b>دسترسی گفتگوی حساب شما محدود شده است.</b>"]
        if status.restriction_reason:
            lines.append(f"📌 <b>دلیل:</b> {html.escape(status.restriction_reason)}")
        if status.expires_at is not None and settings is not None:
            local_exp = to_business_tz(status.expires_at, settings.business_timezone)
            exp_str = local_exp.strftime("%Y-%m-%d %H:%M")
            lines.append(f"⏳ <b>زمان پایان محدودیت:</b> {exp_str} (به وقت تهران)")
        elif status.expires_at is not None:
            exp_str = status.expires_at.strftime("%Y-%m-%d %H:%M")
            lines.append(f"⏳ <b>زمان پایان محدودیت (UTC):</b> {exp_str}")
        lines.append(
            "در مدت محدودیت امکان شروع گفتگوی جدید، ارسال درخواست یا ارسال پیام چت را ندارید."
        )
        return "\n\n".join(lines)

    return (
        "🚫 حساب کاربری شما در حال حاضر غیرفعال یا محدود شده است "
        "و امکان استفاده از این بخش وجود ندارد."
    )
