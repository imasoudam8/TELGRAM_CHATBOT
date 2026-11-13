"""Telegram bot handlers for RBAC administration, moderation queue, wallet, and config."""

import html
import json
import logging
import secrets
import uuid
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.app.bot.handlers.common import MSG_GENERIC_ERROR, resolve_redis_manager
from src.app.bot.handlers.profile import GENDER_LABELS
from src.app.bot.keyboards.admin import (
    CB_ADM_AUDIT_PREFIX,
    CB_ADM_CANCEL,
    CB_ADM_CFG_CONFIRM_PREFIX,
    CB_ADM_CFG_LIST,
    CB_ADM_CFG_SELECT_PREFIX,
    CB_ADM_HOME,
    CB_ADM_MOD_PREFIX,
    CB_ADM_PAIR_BLK,
    CB_ADM_PAIR_MENU,
    CB_ADM_PAIR_UNBLK,
    CB_ADM_REP_MOD_PREFIX,
    CB_ADM_REP_OPEN_PREFIX,
    CB_ADM_REP_PAIRBLK_PREFIX,
    CB_ADM_REP_QUEUE_PREFIX,
    CB_ADM_REP_RESOLVE_PREFIX,
    CB_ADM_ROLE_ADD,
    CB_ADM_ROLE_CONFIRM_PREFIX,
    CB_ADM_ROLE_PICK_PREFIX,
    CB_ADM_ROLE_REVOKE_PREFIX,
    CB_ADM_ROLES,
    CB_ADM_USER_LOOKUP,
    CB_ADM_USER_VIEW_PREFIX,
    CB_ADM_WALLET_CONFIRM_PREFIX,
    CB_ADM_WALLET_CREDIT_PREFIX,
    CB_ADM_WALLET_DEBIT_PREFIX,
    CB_ADM_WALLET_LEDGER_PREFIX,
    CB_ADM_WALLET_MENU,
    CB_ADM_WALLET_OPEN_PREFIX,
    MOD_ACTION_CODES,
    get_admin_cancel_keyboard,
    get_admin_main_menu_keyboard,
    get_audit_logs_keyboard,
    get_confirm_action_keyboard,
    get_pair_block_menu_keyboard,
    get_report_detail_keyboard,
    get_report_queue_keyboard,
    get_role_pick_keyboard,
    get_roles_management_keyboard,
    get_system_configs_keyboard,
    get_user_inspection_keyboard,
    get_wallet_admin_keyboard,
)
from src.app.bot.states import AdminSG
from src.app.config.settings import Settings, get_settings
from src.app.infrastructure.database.base import to_business_tz
from src.app.infrastructure.database.session import DatabaseManager, get_database_manager
from src.app.infrastructure.redis.client import RedisManager
from src.app.modules.admin.config_service import AdminConfigService
from src.app.modules.admin.rbac import ROLE_LABELS_PERSIAN, VALID_ADMIN_ROLES
from src.app.modules.admin.schemas import (
    AdminActorDTO,
    AdminAuditLogPageDTO,
    AdminConflictError,
    AdminNotFoundError,
    AdminPermissionError,
    AdminUserDetailDTO,
    AdminValidationError,
    ModerationOutcomeDTO,
    ReportDetailDTO,
    ReportQueuePageDTO,
)
from src.app.modules.admin.service import MAX_MODERATION_HOURS, AdminService
from src.app.modules.matching.service import RandomMatchService
from src.app.modules.moderation.schemas import REPORT_CATEGORIES
from src.app.modules.notifications.service import NotificationService
from src.app.modules.wallet.schemas import (
    InsufficientBalanceError,
    InvalidAmountError,
    TransactionHistoryPageDTO,
    WalletBalanceDTO,
)

logger = logging.getLogger(__name__)

ADMIN_PAGE_SIZE = 5

MSG_ADMIN_DENIED = "⛔ شما دسترسی به پنل مدیریت ندارید."
MSG_ADMIN_REPLAY_EXPIRED = "❌ این درخواست تأیید منقضی شده یا قبلاً اجرا شده است."

DOMAIN_ERRORS = (
    AdminPermissionError,
    AdminValidationError,
    AdminNotFoundError,
    AdminConflictError,
    InsufficientBalanceError,
    InvalidAmountError,
)

ACCOUNT_STATUS_LABELS: dict[str, str] = {
    "ACTIVE": "✅ فعال",
    "SUSPENDED": "⏳ تعلیق موقت",
    "BANNED": "⛔ مسدود دائم",
    "DELETED": "🗑 حذف‌شده",
}

REPORT_STATUS_LABELS: dict[str, str] = {
    "PENDING": "⏳ در انتظار بررسی",
    "REVIEWED": "👁 بررسی‌شده",
    "ACTIONED": "✅ اقدام‌شده",
    "DISMISSED": "🗑 ردشده",
}

MOD_ACTION_LABELS: dict[str, str] = {
    "WARNING": "⚠️ اخطار رسمی",
    "CHAT_RESTRICTION": "🔇 محدودیت گفتگو",
    "TEMPORARY_SUSPENSION": "⏳ تعلیق موقت حساب",
    "PERMANENT_BAN": "⛔ مسدودسازی دائم",
    "RESTORE": "✅ رفع محدودیت و بازیابی حساب",
}


def _fmt_dt(dt: Any, settings: Settings) -> str:
    if dt is None:
        return "نامشخص"
    local_dt = to_business_tz(dt, settings.business_timezone)
    return local_dt.strftime("%Y-%m-%d %H:%M")


def format_admin_home(actor: AdminActorDTO) -> str:
    """Format role-aware Persian HTML welcome card for authenticated admins."""
    role_label = ROLE_LABELS_PERSIAN.get(actor.role, html.escape(actor.role))
    return (
        "🛡 <b>پنل مدیریت و نظارت تلگرام</b>\n\n"
        f"👤 شناسه عمومی شما: <code>{html.escape(actor.public_id)}</code>\n"
        f"🎖 نقش فعال: <b>{html.escape(role_label)}</b>\n\n"
        "لطفاً عملیات مورد نظر خود را از منوی زیر انتخاب نمایید:"
    )


def format_admin_user_detail(detail: AdminUserDetailDTO, settings: Settings) -> str:
    """Format privacy-safe user operational inspection card without Telegram ID."""
    status_label = ACCOUNT_STATUS_LABELS.get(
        detail.account_status, html.escape(detail.account_status)
    )
    chat_exp = (
        _fmt_dt(detail.chat_restriction_expires_at, settings)
        if detail.chat_restriction_expires_at
        else "نامحدود"
    )
    chat_restr_text = (
        f"🔇 بله (تا {chat_exp})"
        if detail.is_chat_restricted and detail.account_status == "ACTIVE"
        else ("بله (به دلیل وضعیت حساب)" if detail.is_chat_restricted else "خیر")
    )

    profile_summary = "ثبت نشده"
    if detail.display_name is not None:
        gender_lbl = GENDER_LABELS.get(detail.gender or "", detail.gender or "-")
        city_txt = html.escape(detail.city) if detail.city else "ثبت نشده"
        profile_summary = (
            f"{html.escape(detail.display_name)} | {detail.age} سال | {gender_lbl} | {city_txt} "
            f"({html.escape(detail.profile_completion_status or '-')})"
        )

    lines = [
        "🔎 <b>پرونده عملیاتی کاربر</b>",
        "",
        f"🆔 شناسه عمومی: <code>{html.escape(detail.public_id)}</code>",
        f"📊 وضعیت حساب: {status_label}",
        f"🔇 محدودیت گفتگو: {chat_restr_text}",
    ]
    if detail.suspension_reason:
        lines.append(f"📌 دلیل تعلیق/مسدودیت: {html.escape(detail.suspension_reason)}")
    if detail.suspended_until is not None:
        lines.append(f"⏳ پایان تعلیق: {_fmt_dt(detail.suspended_until, settings)}")
    if detail.is_chat_restricted and detail.chat_restriction_reason:
        lines.append(f"📌 دلیل محدودیت گفتگو: {html.escape(detail.chat_restriction_reason)}")

    lines.extend(
        [
            f"👤 خلاصه پروفایل: {profile_summary}",
            f"💬 در گفتگوی فعال: {'بله' if detail.in_active_chat else 'خیر'}",
            f"🚨 گزارش‌های دریافتی: <b>{detail.reports_against_count}</b> | "
            f"گزارش‌های ارسالی: <b>{detail.reports_submitted_count}</b>",
            f"🕒 آخرین فعالیت: {_fmt_dt(detail.last_activity_at, settings)}",
        ]
    )
    if detail.admin_role:
        adm_state = "فعال" if detail.admin_is_active else "غیرفعال"
        lines.append(f"🛡 نقش مدیریتی: <b>{html.escape(detail.admin_role)}</b> ({adm_state})")

    if detail.recent_actions:
        lines.append("\n<b>آخرین سوابق انضباطی:</b>")
        for act in detail.recent_actions:
            act_lbl = MOD_ACTION_LABELS.get(act.action_type, html.escape(act.action_type))
            lines.append(
                f"• {act_lbl} ({_fmt_dt(act.created_at, settings)}): {html.escape(act.reason)}"
            )

    return "\n".join(lines)


def format_report_queue(page: ReportQueuePageDTO, settings: Settings) -> str:
    """Format paginated moderation queue summary in Persian HTML."""
    status_lbl = REPORT_STATUS_LABELS.get(page.status_filter, html.escape(page.status_filter))
    if page.status_filter == "ALL":
        status_lbl = "همه وضعیت‌ها"
    if not page.items:
        return (
            f"📋 <b>صف گزارش‌های تخلف ({status_lbl})</b>\n\n"
            "در حال حاضر گزارشی در این بخش وجود ندارد."
        )

    lines = [f"📋 <b>صف گزارش‌های تخلف ({status_lbl} — مجموع {page.total_count} مورد)</b>\n"]
    for item in page.items:
        cat_lbl = REPORT_CATEGORIES.get(item.category, html.escape(item.category))
        lines.append(
            f"• <b>#{item.id}</b> | {html.escape(cat_lbl)} | "
            f"هدف: <code>{html.escape(item.reported_public_id)}</code> "
            f"({_fmt_dt(item.created_at, settings)})"
        )
    lines.append("\nبرای مشاهده جزئیات و بررسی، روی شماره گزارش کلیک کنید:")
    return "\n".join(lines)


def format_report_detail(detail: ReportDetailDTO, settings: Settings) -> str:
    """Format report triage view and metadata-only evidence holds (no plaintext chat text)."""
    cat_lbl = REPORT_CATEGORIES.get(detail.category, html.escape(detail.category))
    status_lbl = REPORT_STATUS_LABELS.get(detail.status, html.escape(detail.status))
    target_status_lbl = ACCOUNT_STATUS_LABELS.get(
        detail.reported_account_status, html.escape(detail.reported_account_status)
    )
    desc_text = html.escape(detail.description) if detail.description else "<i>بدون توضیحات</i>"

    lines = [
        f"🚨 <b>جزئیات گزارش #{detail.id}</b>",
        "",
        f"📊 وضعیت گزارش: {status_lbl}",
        f"📂 دسته‌بندی: <b>{html.escape(cat_lbl)}</b>",
        f"👤 گزارش‌دهنده: <code>{html.escape(detail.reporter_public_id)}</code>",
        f"🎯 کاربر گزارش‌شده: <code>{html.escape(detail.reported_public_id)}</code> "
        f"({target_status_lbl})",
        f"🔇 محدودیت گفتگوی کاربر هدف: {'بله' if detail.reported_is_chat_restricted else 'خیر'}",
        f"📝 توضیحات گزارش‌دهنده:\n{desc_text}",
        f"🕒 زمان ثبت: {_fmt_dt(detail.created_at, settings)}",
    ]
    if detail.chat_session_id is not None:
        chat_sid = html.escape(str(detail.chat_session_id))
        lines.append(f"💬 شناسه جلسه گفتگو: <code>{chat_sid}</code>")
    if detail.moderator_public_id:
        lines.append(f"🛡 ناظر بررسی‌کننده: <code>{html.escape(detail.moderator_public_id)}</code>")
    if detail.resolution_notes:
        lines.append(f"🗒 یادداشت نهایی: {html.escape(detail.resolution_notes)}")

    holds_count = len(detail.evidence_holds)
    lines.append(f"\n🔒 <b>سوابق شواهد نگهداری‌شده (فقط متادیتا): {holds_count} مورد</b>")
    for hold in detail.evidence_holds[:5]:
        msg_ref = f"msg:{hold.message_id}" if hold.message_id is not None else "session-level"
        mtype = html.escape(hold.message_type) if hold.message_type else "-"
        lines.append(
            f"• <code>#{hold.id}</code> | {msg_ref} ({mtype}) | "
            f"hash: <code>{html.escape(hold.message_content_hash[:12])}…</code>"
        )

    return "\n".join(lines)


def format_admin_wallet_view(
    detail: AdminUserDetailDTO,
    balance: WalletBalanceDTO,
    history: TransactionHistoryPageDTO,
    settings: Settings,
) -> str:
    """Format SUPER_ADMIN wallet & ledger view cleanly with HTML escaping."""
    acc_label = ACCOUNT_STATUS_LABELS.get(detail.account_status, html.escape(detail.account_status))
    lines = [
        f"🪙 <b>مدیریت کیف پول کاربر <code>{html.escape(detail.public_id)}</code></b>",
        "",
        f"💎 سکه‌های خریداری‌شده: <b>{balance.purchased_coins}</b>",
        f"🎁 سهمیه رایگان فعال: <b>{balance.free_coins}</b>",
        f"💰 مجموع قابل مصرف: <b>{balance.total_spendable_coins}</b>",
        f"📊 وضعیت حساب: {acc_label}",
        "",
        f"📜 <b>دفترکل تراکنش‌ها ({history.total_count} مورد):</b>",
    ]
    if not history.items:
        lines.append("• هیچ تراکنشی ثبت نشده است.")
    else:
        for item in history.items:
            total_before = item.purchased_balance_before + item.free_balance_before
            total_after = item.purchased_balance_after + item.free_balance_after
            sign = "+" if total_after >= total_before else "-"
            tx_type = html.escape(item.transaction_type)
            cr_type = html.escape(item.credit_type)
            lines.append(
                f"• #{item.id} | {tx_type} ({cr_type}): "
                f"<b>{sign}{item.amount}</b> | {total_before}⬅️{total_after}\n"
                f"  دلیل: {html.escape(item.reason)} | {_fmt_dt(item.created_at, settings)}"
            )
    return "\n".join(lines)


def format_audit_logs_page(page: AdminAuditLogPageDTO, settings: Settings) -> str:
    """Format paginated append-only admin audit logs safely."""
    if not page.items:
        return "📜 <b>گزارش ممیزی عملیات ادمین</b>\n\nهیچ رکورد ممیزی ثبت نشده است."

    lines = [f"📜 <b>گزارش ممیزی عملیات ادمین ({page.total_count} رکورد)</b>\n"]
    for item in page.items:
        after_preview = ""
        if item.payload_after:
            raw_after = json.dumps(item.payload_after, ensure_ascii=False, separators=(",", ":"))
            if len(raw_after) > 120:
                raw_after = raw_after[:117] + "…"
            after_preview = f"\n  تغییر: <code>{html.escape(raw_after)}</code>"

        lines.append(
            f"• <b>#{item.id}</b> | <code>{html.escape(item.action)}</code>\n"
            f"  ادمین: <code>{html.escape(item.admin_public_id)}</code> | "
            f"هدف: <code>{html.escape(item.target_type)}:{html.escape(item.target_id)}</code>\n"
            f"  دلیل: {html.escape(item.reason)}{after_preview}\n"
            f"  زمان: {_fmt_dt(item.created_at, settings)}"
        )
    return "\n".join(lines)


async def _get_admin_or_deny_message(
    message: Message,
    state: FSMContext,
    admin_service: AdminService,
) -> AdminActorDTO | None:
    if message.from_user is None:
        return None
    actor = await admin_service.get_active_admin_context(message.from_user.id)
    if actor is None:
        await state.clear()
        await message.answer(MSG_ADMIN_DENIED)
        return None
    return actor


async def _get_admin_or_deny_callback(
    callback: CallbackQuery,
    state: FSMContext,
    admin_service: AdminService,
) -> AdminActorDTO | None:
    if callback.from_user is None:
        await callback.answer()
        return None
    actor = await admin_service.get_active_admin_context(callback.from_user.id)
    if actor is None:
        await callback.answer(MSG_ADMIN_DENIED, show_alert=True)
        await state.clear()
        if isinstance(callback.message, Message):
            await callback.message.answer(MSG_ADMIN_DENIED)
        return None
    return actor


async def _notify_moderation_outcome(
    *,
    bot: Any,
    outcome: ModerationOutcomeDTO,
    db_manager: DatabaseManager,
    redis_manager: RedisManager | None,
    settings: Settings,
) -> None:
    """Deliver post-commit Redis queue cleanup and privacy-safe notifications."""
    if outcome.requires_queue_removal:
        try:
            await RandomMatchService(db_manager, resolve_redis_manager(redis_manager)).leave_queue(
                outcome.target_user_id
            )
        except Exception as exc:
            logger.warning("Redis queue cleanup skipped (%s)", type(exc).__name__)

    notifier = NotificationService()
    if outcome.target_telegram_id:
        exp_text = ""
        if outcome.expires_at is not None:
            exp_formatted = _fmt_dt(outcome.expires_at, settings)
            exp_text = f"\n⏳ <b>زمان پایان:</b> {exp_formatted} (به وقت تهران)"

        if outcome.action_type == "WARNING":
            msg_text = (
                "⚠️ <b>اخطار انضباطی</b>\n\n"
                "لطفاً قوانین و مقررات گفتگوی محترمانه در ربات را رعایت فرمایید. "
                "تکرار تخلف می‌تواند منجر به محدودیت گفتگو یا تعلیق حساب شود."
            )
        elif outcome.action_type == "CHAT_RESTRICTION":
            msg_text = (
                "🔇 <b>دسترسی گفتگوی شما محدود شد.</b>\n\n"
                "امکان شرکت در گفتگوی تصادفی، ارسال/پذیرش درخواست مستقیم و ارسال پیام "
                "تا زمان رفع محدودیت غیرفعال است."
                f"{exp_text}"
            )
        elif outcome.action_type == "TEMPORARY_SUSPENSION":
            msg_text = (
                "⛔ <b>حساب کاربری شما به صورت موقت تعلیق شد.</b>\n\n"
                "دسترسی به امکانات ربات تا پایان دوره تعلیق متوقف است."
                f"{exp_text}"
            )
        elif outcome.action_type == "PERMANENT_BAN":
            msg_text = (
                "🚫 <b>حساب کاربری شما به صورت دائم مسدود شد.</b>\n\n"
                "به دلیل نقض قوانین، دسترسی این حساب به ربات قطع گردیده است."
            )
        else:
            msg_text = (
                "✅ <b>محدودیت حساب کاربری شما برطرف شد.</b>\n\n"
                "اکنون می‌توانید مجدداً از امکانات ربات استفاده نمایید."
            )

        await notifier.send_safe_message(bot, outcome.target_telegram_id, msg_text)

    if outcome.terminated_partner_telegram_id:
        await notifier.send_safe_message(
            bot,
            outcome.terminated_partner_telegram_id,
            "🔒 گفتگوی فعال شما توسط تیم نظارت پایان داده شد.",
        )


# =============================================================================
# Entry, Home & Cancel
# =============================================================================
async def handle_admin_command(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Handle /admin command with strict database-backed RBAC check."""
    if message.from_user is None:
        return
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_message(message, state, admin_service)
        if actor is None:
            return
        await state.clear()
        await message.answer(
            format_admin_home(actor),
            reply_markup=get_admin_main_menu_keyboard(actor.role),
        )
    except Exception as exc:
        logger.error("Error handling /admin command: %s", type(exc).__name__)
        await message.answer(MSG_GENERIC_ERROR)


async def handle_admin_home_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Return to role-tailored admin panel home."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return
        await callback.answer()
        await state.clear()
        await callback.message.answer(
            format_admin_home(actor),
            reply_markup=get_admin_main_menu_keyboard(actor.role),
        )
    except Exception as exc:
        logger.error("Error opening admin home: %s", type(exc).__name__)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_admin_cancel_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Cancel ongoing admin FSM operation cleanly."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None:
            return
        await callback.answer("عملیات لغو شد.")
        await state.clear()
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await callback.message.answer(
                "عملیات لغو شد.\n\n" + format_admin_home(actor),
                reply_markup=get_admin_main_menu_keyboard(actor.role),
            )
    except Exception as exc:
        logger.error("Error cancelling admin FSM: %s", type(exc).__name__)
        await callback.answer()


# =============================================================================
# Report Queue, Inspection & Resolution
# =============================================================================
async def handle_admin_report_queue_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Show paginated reports queue filtered by status."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return

        payload = (callback.data or "").replace(CB_ADM_REP_QUEUE_PREFIX, "", 1)
        parts = payload.split(":", 1)
        status_filter = parts[0] if parts and parts[0] else "PENDING"
        try:
            offset = max(0, int(parts[1])) if len(parts) > 1 else 0
        except ValueError:
            offset = 0

        await callback.answer()
        page = await admin_service.list_reports(
            callback.from_user.id,  # type: ignore[union-attr]
            status_filter=status_filter,
            offset=offset,
            limit=ADMIN_PAGE_SIZE,
        )
        await callback.message.answer(
            format_report_queue(page, resolved_settings),
            reply_markup=get_report_queue_keyboard(page),
        )
    except DOMAIN_ERRORS as err:
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.answer(f"❌ {html.escape(str(err))}")
    except Exception as exc:
        logger.error("Error listing admin reports: %s", type(exc).__name__)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_admin_report_open_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Open full triage view for a specific report."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return

        raw_id = (callback.data or "").replace(CB_ADM_REP_OPEN_PREFIX, "", 1).strip()
        try:
            report_id = int(raw_id)
        except ValueError:
            await callback.answer("شناسه گزارش نامعتبر است.", show_alert=True)
            return

        await callback.answer()
        detail = await admin_service.get_report_detail(callback.from_user.id, report_id)  # type: ignore[union-attr]
        await callback.message.answer(
            format_report_detail(detail, resolved_settings),
            reply_markup=get_report_detail_keyboard(detail, actor.role),
        )
    except DOMAIN_ERRORS as err:
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.answer(f"❌ {html.escape(str(err))}")
    except Exception as exc:
        logger.error("Error opening report detail: %s", type(exc).__name__)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_admin_report_resolve_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Prompt moderator/super-admin for mandatory resolution notes before closing a report."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return

        if actor.role not in {"MODERATOR", "SUPER_ADMIN"}:
            await callback.answer("دسترسی کافی نیست.", show_alert=True)
            await callback.message.answer("⛔ نقش شما مجاز به تعیین تکلیف گزارش نیست.")
            return

        payload = (callback.data or "").replace(CB_ADM_REP_RESOLVE_PREFIX, "", 1)
        parts = payload.split(":", 1)
        if len(parts) != 2:
            await callback.answer("درخواست نامعتبر است.", show_alert=True)
            return

        new_status = parts[0].strip().upper()
        try:
            report_id = int(parts[1])
        except ValueError:
            await callback.answer("شناسه گزارش نامعتبر است.", show_alert=True)
            return

        await callback.answer()
        await state.clear()
        await state.set_state(AdminSG.waiting_for_report_notes)
        await state.update_data(resolve_report_id=report_id, resolve_status=new_status)
        status_lbl = REPORT_STATUS_LABELS.get(new_status, html.escape(new_status))
        await callback.message.answer(
            f"📝 تغییر وضعیت گزارش <code>#{report_id}</code> به <b>{status_lbl}</b>\n\n"
            "لطفاً یادداشت بررسی و دلیل تصمیم خود را وارد کنید (الزامی):",
            reply_markup=get_admin_cancel_keyboard(),
        )
    except Exception as exc:
        logger.error("Error starting report resolution: %s", type(exc).__name__)
        await callback.answer()


async def handle_admin_report_notes_input(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Resolve PENDING report with mandatory notes."""
    if message.from_user is None:
        return
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_message(message, state, admin_service)
        if actor is None:
            return

        notes = (message.text or "").strip()
        if not notes:
            await message.answer(
                "❌ ثبت یادداشت بررسی الزامی است. لطفاً متن توضیح را ارسال کنید:",
                reply_markup=get_admin_cancel_keyboard(),
            )
            return

        data = await state.get_data()
        report_id = data.get("resolve_report_id")
        new_status = data.get("resolve_status")
        if not report_id or not new_status:
            await state.clear()
            await message.answer("❌ اطلاعات گزارش در وضعیت فعلی یافت نشد.")
            return

        detail, audit_id = await admin_service.resolve_report(
            message.from_user.id,
            int(report_id),
            new_status=str(new_status),
            resolution_notes=notes,
        )
        await state.clear()
        await message.answer(
            f"✅ گزارش <code>#{detail.id}</code> با وضعیت <b>{html.escape(detail.status)}</b> "
            f"ثبت و ممیزی شد (Audit <code>#{audit_id}</code>).\n\n"
            + format_report_detail(detail, resolved_settings),
            reply_markup=get_report_detail_keyboard(detail, actor.role),
        )
    except DOMAIN_ERRORS as err:
        if isinstance(err, (AdminConflictError, AdminNotFoundError, AdminPermissionError)):
            await state.clear()
        await message.answer(f"❌ {html.escape(str(err))}")
    except Exception as exc:
        logger.error("Error resolving report: %s", type(exc).__name__)
        await state.clear()
        await message.answer(MSG_GENERIC_ERROR)


# =============================================================================
# User Inspection
# =============================================================================
async def handle_admin_user_lookup_prompt(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Ask admin to enter a target user's public_id."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return
        await callback.answer()
        await state.clear()
        await state.set_state(AdminSG.waiting_for_user_public_id)
        await callback.message.answer(
            "🔎 <b>جستجوی پرونده کاربر</b>\n\n"
            "لطفاً شناسه عمومی کاربر (مثلاً <code>usr_xxxxxxxxxxxx</code>) را ارسال نمایید:",
            reply_markup=get_admin_cancel_keyboard(),
        )
    except Exception as exc:
        logger.error("Error prompting user lookup: %s", type(exc).__name__)
        await callback.answer()


async def handle_admin_user_lookup_input(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Look up user by public_id and render role-filtered action keyboard."""
    if message.from_user is None:
        return
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_message(message, state, admin_service)
        if actor is None:
            return
        pid = (message.text or "").strip()
        if not pid:
            await message.answer(
                "❌ شناسه عمومی نمی‌تواند خالی باشد:",
                reply_markup=get_admin_cancel_keyboard(),
            )
            return

        detail = await admin_service.lookup_user_by_public_id(message.from_user.id, pid)
        await state.clear()
        await message.answer(
            format_admin_user_detail(detail, resolved_settings),
            reply_markup=get_user_inspection_keyboard(detail, actor.role),
        )
    except DOMAIN_ERRORS as err:
        if isinstance(err, AdminPermissionError):
            await state.clear()
        await message.answer(f"❌ {html.escape(str(err))}")
    except Exception as exc:
        logger.error("Error looking up user in admin panel: %s", type(exc).__name__)
        await state.clear()
        await message.answer(MSG_GENERIC_ERROR)


async def handle_admin_user_view_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Open user inspection card directly via inline button."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return
        target_pid = (callback.data or "").replace(CB_ADM_USER_VIEW_PREFIX, "", 1).strip()
        await callback.answer()
        detail = await admin_service.lookup_user_by_public_id(callback.from_user.id, target_pid)  # type: ignore[union-attr]
        await callback.message.answer(
            format_admin_user_detail(detail, resolved_settings),
            reply_markup=get_user_inspection_keyboard(detail, actor.role),
        )
    except DOMAIN_ERRORS as err:
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.answer(f"❌ {html.escape(str(err))}")
    except Exception as exc:
        logger.error("Error viewing user detail: %s", type(exc).__name__)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.answer(MSG_GENERIC_ERROR)


# =============================================================================
# User Moderation Actions & Account Restoration
# =============================================================================
async def _begin_moderation_flow(
    callback: CallbackQuery,
    state: FSMContext,
    actor: AdminActorDTO,
    *,
    action_code: str,
    target_public_id: str,
    report_id: int | None,
) -> None:
    action_type = MOD_ACTION_CODES.get(action_code)
    if action_type is None or not target_public_id:
        await callback.answer("عملیات نامعتبر است.", show_alert=True)
        return

    msg = callback.message
    if not isinstance(msg, Message):
        await callback.answer()
        return

    if action_type in {
        "CHAT_RESTRICTION",
        "TEMPORARY_SUSPENSION",
        "RESTORE",
    } and actor.role not in {"MODERATOR", "SUPER_ADMIN"}:
        await callback.answer("دسترسی کافی نیست.", show_alert=True)
        await msg.answer("⛔ نقش شما مجاز به اجرای این اقدام انضباطی نیست.")
        return

    if action_type == "PERMANENT_BAN" and actor.role != "SUPER_ADMIN":
        await callback.answer("دسترسی کافی نیست.", show_alert=True)
        await msg.answer("⛔ مسدودسازی دائم فقط در اختیار سوپر ادمین است.")
        return

    await callback.answer()
    await state.clear()
    action_label = MOD_ACTION_LABELS.get(action_type, action_type)
    escaped_label = html.escape(action_label)
    escaped_pid = html.escape(target_public_id)

    if action_type in {"TEMPORARY_SUSPENSION", "CHAT_RESTRICTION"}:
        await state.set_state(AdminSG.waiting_for_moderation_duration)
        await state.update_data(
            mod_action_type=action_type,
            mod_target_pid=target_public_id,
            mod_report_id=report_id,
        )
        zero_hint = (
            " (یا عدد <code>0</code> برای محدودیت بدون انقضا)"
            if action_type == "CHAT_RESTRICTION"
            else ""
        )
        await msg.answer(
            f"🛡 <b>{escaped_label}</b> برای <code>{escaped_pid}</code>\n\n"
            f"لطفاً مدت زمان را به <b>ساعت</b> وارد کنید "
            f"(عدد صحیح بین ۱ تا {MAX_MODERATION_HOURS}){zero_hint}:",
            reply_markup=get_admin_cancel_keyboard(),
        )
        return

    await state.set_state(AdminSG.waiting_for_moderation_reason)
    await state.update_data(
        mod_action_type=action_type,
        mod_target_pid=target_public_id,
        mod_report_id=report_id,
        mod_duration_hours=None,
    )
    await msg.answer(
        f"🛡 <b>{escaped_label}</b> برای <code>{escaped_pid}</code>\n\n"
        "لطفاً دلیل دقیق ثبت این اقدام را وارد نمایید (الزامی و در گزارش ممیزی ثبت می‌شود):",
        reply_markup=get_admin_cancel_keyboard(),
    )


async def handle_admin_user_moderation_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Start moderation action from user inspection view."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None:
            return
        payload = (callback.data or "").replace(CB_ADM_MOD_PREFIX, "", 1)
        parts = payload.split(":", 1)
        if len(parts) != 2:
            await callback.answer("پارامتر عملیات نامعتبر است.", show_alert=True)
            return
        await _begin_moderation_flow(
            callback,
            state,
            actor,
            action_code=parts[0].strip().upper(),
            target_public_id=parts[1].strip(),
            report_id=None,
        )
    except Exception as exc:
        logger.error("Error initiating user moderation: %s", type(exc).__name__)
        await callback.answer()


async def handle_admin_report_moderation_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Start moderation action linked directly to a specific report."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None:
            return
        payload = (callback.data or "").replace(CB_ADM_REP_MOD_PREFIX, "", 1)
        parts = payload.split(":", 2)
        if len(parts) != 3:
            await callback.answer("پارامتر عملیات نامعتبر است.", show_alert=True)
            return
        try:
            report_id = int(parts[1])
        except ValueError:
            await callback.answer("شناسه گزارش نامعتبر است.", show_alert=True)
            return
        await _begin_moderation_flow(
            callback,
            state,
            actor,
            action_code=parts[0].strip().upper(),
            target_public_id=parts[2].strip(),
            report_id=report_id,
        )
    except Exception as exc:
        logger.error("Error initiating report moderation: %s", type(exc).__name__)
        await callback.answer()


async def handle_admin_moderation_duration_input(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Validate moderation duration in hours and prompt for mandatory reason."""
    if message.from_user is None:
        return
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_message(message, state, admin_service)
        if actor is None:
            return

        data = await state.get_data()
        action_type = data.get("mod_action_type")
        target_pid = data.get("mod_target_pid")
        if not action_type or not target_pid:
            await state.clear()
            await message.answer("❌ اطلاعات عملیات یافت نشد.")
            return

        raw_txt = (message.text or "").strip()
        try:
            hours = int(raw_txt)
        except ValueError:
            await message.answer(
                "❌ لطفاً مدت زمان را فقط به صورت عدد صحیح ساعت ارسال کنید:",
                reply_markup=get_admin_cancel_keyboard(),
            )
            return

        duration_hours: int | None
        if action_type == "CHAT_RESTRICTION" and hours == 0:
            duration_hours = None
        elif hours < 1 or hours > MAX_MODERATION_HOURS:
            await message.answer(
                f"❌ مدت باید بین ۱ تا {MAX_MODERATION_HOURS} ساعت باشد:",
                reply_markup=get_admin_cancel_keyboard(),
            )
            return
        else:
            duration_hours = hours

        await state.update_data(mod_duration_hours=duration_hours)
        await state.set_state(AdminSG.waiting_for_moderation_reason)
        await message.answer(
            "📝 اکنون دلیل دقیق ثبت این اقدام را وارد نمایید (الزامی و در گزارش ممیزی ثبت می‌شود):",
            reply_markup=get_admin_cancel_keyboard(),
        )
    except Exception as exc:
        logger.error("Error reading moderation duration: %s", type(exc).__name__)
        await state.clear()
        await message.answer(MSG_GENERIC_ERROR)


async def handle_admin_moderation_reason_input(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    redis_manager: RedisManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Execute moderation or restore action with RBAC check, audit logging, and side effects."""
    if message.from_user is None:
        return
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_message(message, state, admin_service)
        if actor is None:
            return

        reason = (message.text or "").strip()
        if not reason:
            await message.answer(
                "❌ ذکر دلیل الزامی است. لطفاً دلیل را ارسال نمایید:",
                reply_markup=get_admin_cancel_keyboard(),
            )
            return

        data = await state.get_data()
        action_type = data.get("mod_action_type")
        target_pid = data.get("mod_target_pid")
        report_id = data.get("mod_report_id")
        duration_hours = data.get("mod_duration_hours")
        if not action_type or not target_pid:
            await state.clear()
            await message.answer("❌ اطلاعات عملیات انضباطی ناقص است.")
            return

        if action_type == "RESTORE":
            outcome = await admin_service.restore_user_access(
                message.from_user.id,
                target_public_id=str(target_pid),
                reason=reason,
            )
        else:
            outcome = await admin_service.apply_moderation_action(
                message.from_user.id,
                target_public_id=str(target_pid),
                action_type=str(action_type),
                reason=reason,
                duration_hours=duration_hours,
                report_id=int(report_id) if report_id is not None else None,
            )

        await state.clear()
        await _notify_moderation_outcome(
            bot=message.bot,
            outcome=outcome,
            db_manager=resolved_db,
            redis_manager=redis_manager,
            settings=resolved_settings,
        )

        act_lbl = MOD_ACTION_LABELS.get(outcome.action_type, html.escape(outcome.action_type))
        extra = ""
        if outcome.terminated_chat_id is not None:
            extra = "\n🔒 گفتگوی فعال کاربر نیز پایان داده شد."

        await message.answer(
            f"✅ <b>اقدام با موفقیت ثبت شد.</b>\n\n"
            f"📌 نوع اقدام: <b>{html.escape(act_lbl)}</b>\n"
            f"🎯 کاربر هدف: <code>{html.escape(outcome.target_public_id)}</code>\n"
            f"📊 وضعیت جدید: <code>{html.escape(outcome.target_status)}</code>\n"
            f"📜 شماره ممیزی: <code>#{outcome.audit_log_id}</code>"
            f"{extra}",
            reply_markup=get_admin_main_menu_keyboard(actor.role),
        )
    except DOMAIN_ERRORS as err:
        await state.clear()
        await message.answer(f"❌ {html.escape(str(err))}")
    except Exception as exc:
        logger.error("Error executing moderation action: %s", type(exc).__name__)
        await state.clear()
        await message.answer(MSG_GENERIC_ERROR)


# =============================================================================
# Moderator User-Pair Block & Unblock
# =============================================================================
async def handle_admin_pair_menu_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Open pair block/unblock menu for MODERATOR and SUPER_ADMIN."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return
        if actor.role not in {"MODERATOR", "SUPER_ADMIN"}:
            await callback.answer("دسترسی کافی نیست.", show_alert=True)
            await callback.message.answer("⛔ این بخش فقط برای ناظر و سوپر ادمین فعال است.")
            return
        await callback.answer()
        await callback.message.answer(
            "🚫 <b>مدیریت مسدودسازی ارتباط دو کاربر</b>\n\n"
            "می‌توانید ارتباط دو کاربر را مسدود کرده (و گفتگوی فعال یا درخواست‌های معلق را ببندید) "
            "یا یک مسدودسازی قبلی را رفع نمایید:",
            reply_markup=get_pair_block_menu_keyboard(),
        )
    except Exception as exc:
        logger.error("Error opening pair block menu: %s", type(exc).__name__)
        await callback.answer()


async def handle_admin_pair_block_start_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Start manual pair block prompt."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return
        if actor.role not in {"MODERATOR", "SUPER_ADMIN"}:
            await callback.answer("دسترسی کافی نیست.", show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await state.set_state(AdminSG.waiting_for_pair_block_input)
        await callback.message.answer(
            "🚫 <b>مسدودسازی ارتباط دو کاربر</b>\n\n"
            "لطفاً دو شناسه عمومی و دلیل را در یک خط و با فاصله وارد کنید:\n"
            "<code>usr_user1 usr_user2 دلیل مسدودسازی</code>",
            reply_markup=get_admin_cancel_keyboard(),
        )
    except Exception as exc:
        logger.error("Error starting pair block prompt: %s", type(exc).__name__)
        await callback.answer()


async def handle_admin_report_pair_block_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Pre-fill pair block directly from a report and ask only for reason."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return
        if actor.role not in {"MODERATOR", "SUPER_ADMIN"}:
            await callback.answer("دسترسی کافی نیست.", show_alert=True)
            return
        payload = (callback.data or "").replace(CB_ADM_REP_PAIRBLK_PREFIX, "", 1)
        parts = payload.split(":", 2)
        if len(parts) != 3:
            await callback.answer("پارامتر نامعتبر است.", show_alert=True)
            return
        try:
            report_id = int(parts[0])
        except ValueError:
            await callback.answer("شناسه گزارش نامعتبر است.", show_alert=True)
            return
        reporter_pid, reported_pid = parts[1].strip(), parts[2].strip()
        await callback.answer()
        await state.clear()
        await state.set_state(AdminSG.waiting_for_pair_block_input)
        await state.update_data(
            pair_report_id=report_id,
            pair_blocker_pid=reporter_pid,
            pair_blocked_pid=reported_pid,
        )
        await callback.message.answer(
            f"🚫 مسدود کردن ارتباط بین <code>{html.escape(reporter_pid)}</code> و "
            f"<code>{html.escape(reported_pid)}</code> (گزارش #{report_id})\n\n"
            "لطفاً دلیل ثبت این مسدودسازی را ارسال کنید:",
            reply_markup=get_admin_cancel_keyboard(),
        )
    except Exception as exc:
        logger.error("Error starting report pair block: %s", type(exc).__name__)
        await callback.answer()


async def handle_admin_pair_block_input(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Execute admin_block_users and close active chat if present."""
    if message.from_user is None:
        return
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_message(message, state, admin_service)
        if actor is None:
            return
        text = (message.text or "").strip()
        data = await state.get_data()
        pre_blocker = data.get("pair_blocker_pid")
        pre_blocked = data.get("pair_blocked_pid")
        report_id = data.get("pair_report_id")

        if pre_blocker and pre_blocked:
            blocker_pid = str(pre_blocker)
            blocked_pid = str(pre_blocked)
            reason = text
        else:
            parts = text.split(maxsplit=2)
            if len(parts) < 3:
                await message.answer(
                    "❌ فرمت نادرست است. به شکل زیر وارد کنید:\n"
                    "<code>usr_user1 usr_user2 دلیل مسدودسازی</code>",
                    reply_markup=get_admin_cancel_keyboard(),
                )
                return
            blocker_pid, blocked_pid, reason = parts[0], parts[1], parts[2]

        outcome = await admin_service.admin_block_users(
            message.from_user.id,
            blocker_public_id=blocker_pid,
            blocked_public_id=blocked_pid,
            reason=reason,
            report_id=int(report_id) if report_id is not None else None,
        )
        await state.clear()

        if outcome.terminated_chat_id is not None and outcome.participant_telegram_ids:
            notifier = NotificationService()
            for tg_id in outcome.participant_telegram_ids:
                await notifier.send_safe_message(
                    message.bot,
                    tg_id,
                    "🔒 گفتگوی فعال شما توسط تیم نظارت پایان داده شد.",
                )

        await message.answer(
            f"✅ ارتباط بین <code>{html.escape(outcome.blocker_public_id)}</code> و "
            f"<code>{html.escape(outcome.blocked_public_id)}</code> مسدود شد "
            f"(Audit <code>#{outcome.audit_log_id}</code>).",
            reply_markup=get_admin_main_menu_keyboard(actor.role),
        )
    except DOMAIN_ERRORS as err:
        await state.clear()
        await message.answer(f"❌ {html.escape(str(err))}")
    except Exception as exc:
        logger.error("Error blocking user pair: %s", type(exc).__name__)
        await state.clear()
        await message.answer(MSG_GENERIC_ERROR)


async def handle_admin_pair_unblock_start_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Start pair unblock prompt."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return
        if actor.role not in {"MODERATOR", "SUPER_ADMIN"}:
            await callback.answer("دسترسی کافی نیست.", show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await state.set_state(AdminSG.waiting_for_pair_unblock_input)
        await callback.message.answer(
            "🔓 <b>رفع مسدودیت ارتباط دو کاربر</b>\n\n"
            "لطفاً شناسه مسدودکننده، شناسه مسدودشده و دلیل را در یک خط وارد کنید:\n"
            "<code>usr_blocker usr_blocked دلیل رفع مسدودیت</code>",
            reply_markup=get_admin_cancel_keyboard(),
        )
    except Exception as exc:
        logger.error("Error starting pair unblock prompt: %s", type(exc).__name__)
        await callback.answer()


async def handle_admin_pair_unblock_input(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Execute admin_unblock_users."""
    if message.from_user is None:
        return
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_message(message, state, admin_service)
        if actor is None:
            return
        parts = (message.text or "").strip().split(maxsplit=2)
        if len(parts) < 3:
            await message.answer(
                "❌ فرمت نادرست است. به شکل زیر وارد کنید:\n"
                "<code>usr_blocker usr_blocked دلیل رفع مسدودیت</code>",
                reply_markup=get_admin_cancel_keyboard(),
            )
            return
        outcome = await admin_service.admin_unblock_users(
            message.from_user.id,
            blocker_public_id=parts[0],
            blocked_public_id=parts[1],
            reason=parts[2],
        )
        await state.clear()
        await message.answer(
            f"✅ مسدودیت ارتباط <code>{html.escape(outcome.blocker_public_id)}</code> ⬅️ "
            f"<code>{html.escape(outcome.blocked_public_id)}</code> برداشته شد "
            f"(Audit <code>#{outcome.audit_log_id}</code>).",
            reply_markup=get_admin_main_menu_keyboard(actor.role),
        )
    except DOMAIN_ERRORS as err:
        await state.clear()
        await message.answer(f"❌ {html.escape(str(err))}")
    except Exception as exc:
        logger.error("Error unblocking user pair: %s", type(exc).__name__)
        await state.clear()
        await message.answer(MSG_GENERIC_ERROR)


# =============================================================================
# SUPER_ADMIN Wallet Management (with Two-Step Confirmation)
# =============================================================================
async def handle_admin_wallet_menu_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Prompt SUPER_ADMIN for user public_id to inspect or adjust wallet."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return
        if actor.role != "SUPER_ADMIN":
            await callback.answer("دسترسی کافی نیست.", show_alert=True)
            await callback.message.answer("⛔ مدیریت کیف پول کاربران فقط در اختیار سوپر ادمین است.")
            return
        await callback.answer()
        await state.clear()
        await state.set_state(AdminSG.waiting_for_wallet_public_id)
        await callback.message.answer(
            "🪙 <b>مدیریت کیف پول کاربر</b>\n\n"
            "لطفاً شناسه عمومی کاربر (مثلاً <code>usr_xxxxxxxxxxxx</code>) را وارد کنید:",
            reply_markup=get_admin_cancel_keyboard(),
        )
    except Exception as exc:
        logger.error("Error prompting wallet user id: %s", type(exc).__name__)
        await callback.answer()


async def handle_admin_wallet_public_id_input(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Inspect entered user's wallet and display SUPER_ADMIN actions."""
    if message.from_user is None:
        return
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_message(message, state, admin_service)
        if actor is None:
            return
        pid = (message.text or "").strip()
        detail, balance, history = await admin_service.inspect_user_wallet(
            message.from_user.id, pid, offset=0, limit=ADMIN_PAGE_SIZE
        )
        await state.clear()
        await message.answer(
            format_admin_wallet_view(detail, balance, history, resolved_settings),
            reply_markup=get_wallet_admin_keyboard(detail.public_id, history),
        )
    except DOMAIN_ERRORS as err:
        if isinstance(err, AdminPermissionError):
            await state.clear()
        await message.answer(f"❌ {html.escape(str(err))}")
    except Exception as exc:
        logger.error("Error inspecting wallet via input: %s", type(exc).__name__)
        await state.clear()
        await message.answer(MSG_GENERIC_ERROR)


async def handle_admin_wallet_open_or_ledger_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Inspect user's wallet or paginate ledger via inline callback."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return
        raw = callback.data or ""
        if raw.startswith(CB_ADM_WALLET_OPEN_PREFIX):
            pid = raw.replace(CB_ADM_WALLET_OPEN_PREFIX, "", 1).strip()
            offset = 0
        else:
            payload = raw.replace(CB_ADM_WALLET_LEDGER_PREFIX, "", 1)
            parts = payload.split(":", 1)
            pid = parts[0].strip()
            try:
                offset = max(0, int(parts[1])) if len(parts) > 1 else 0
            except ValueError:
                offset = 0

        await callback.answer()
        detail, balance, history = await admin_service.inspect_user_wallet(
            callback.from_user.id,  # type: ignore[union-attr]
            pid,
            offset=offset,
            limit=ADMIN_PAGE_SIZE,
        )
        await callback.message.answer(
            format_admin_wallet_view(detail, balance, history, resolved_settings),
            reply_markup=get_wallet_admin_keyboard(detail.public_id, history),
        )
    except DOMAIN_ERRORS as err:
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.answer(f"❌ {html.escape(str(err))}")
    except Exception as exc:
        logger.error("Error opening user wallet: %s", type(exc).__name__)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_admin_wallet_adjust_start_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Start SUPER_ADMIN wallet credit or debit flow."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return
        if actor.role != "SUPER_ADMIN":
            await callback.answer("دسترسی کافی نیست.", show_alert=True)
            await callback.message.answer("⛔ تغییر موجودی کیف پول فقط توسط سوپر ادمین مجاز است.")
            return

        raw = callback.data or ""
        if raw.startswith(CB_ADM_WALLET_CREDIT_PREFIX):
            op = "CREDIT"
            pid = raw.replace(CB_ADM_WALLET_CREDIT_PREFIX, "", 1).strip()
            label = "➕ افزایش سکه خریداری‌شده"
        else:
            op = "DEBIT"
            pid = raw.replace(CB_ADM_WALLET_DEBIT_PREFIX, "", 1).strip()
            label = "➖ کسر سکه خریداری‌شده"

        await callback.answer()
        await state.clear()
        await state.set_state(AdminSG.waiting_for_wallet_amount)
        await state.update_data(wallet_op=op, wallet_target_pid=pid)
        await callback.message.answer(
            f"🪙 <b>{label}</b> برای <code>{html.escape(pid)}</code>\n\n"
            "لطفاً تعداد سکه را به صورت عدد صحیح مثبت وارد کنید:",
            reply_markup=get_admin_cancel_keyboard(),
        )
    except Exception as exc:
        logger.error("Error starting wallet adjustment: %s", type(exc).__name__)
        await callback.answer()


async def handle_admin_wallet_amount_input(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Validate positive integer coin amount and prompt for mandatory reason."""
    if message.from_user is None:
        return
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_message(message, state, admin_service)
        if actor is None:
            return
        try:
            amount = int((message.text or "").strip())
        except ValueError:
            await message.answer(
                "❌ لطفاً فقط یک عدد صحیح مثبت وارد کنید:",
                reply_markup=get_admin_cancel_keyboard(),
            )
            return
        if amount <= 0:
            await message.answer(
                "❌ تعداد سکه باید بزرگ‌تر از صفر باشد:",
                reply_markup=get_admin_cancel_keyboard(),
            )
            return

        await state.update_data(wallet_amount=amount)
        await state.set_state(AdminSG.waiting_for_wallet_reason)
        await message.answer(
            "📝 لطفاً دلیل دقیق این تغییر مالی را وارد کنید (در دفترکل و گزارش ممیزی ثبت می‌شود):",
            reply_markup=get_admin_cancel_keyboard(),
        )
    except Exception as exc:
        logger.error("Error reading wallet amount: %s", type(exc).__name__)
        await state.clear()
        await message.answer(MSG_GENERIC_ERROR)


async def handle_admin_wallet_reason_input(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Show two-step confirmation card with one-time token for wallet credit/debit."""
    if message.from_user is None:
        return
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_message(message, state, admin_service)
        if actor is None:
            return
        reason = (message.text or "").strip()
        if not reason:
            await message.answer(
                "❌ ذکر دلیل الزامی است:",
                reply_markup=get_admin_cancel_keyboard(),
            )
            return

        data = await state.get_data()
        op = data.get("wallet_op")
        pid = data.get("wallet_target_pid")
        amount = data.get("wallet_amount")
        if not op or not pid or not amount:
            await state.clear()
            await message.answer("❌ اطلاعات عملیات کیف پول ناقص است.")
            return

        token = secrets.token_hex(6)
        idem_key = f"tg_adm_wal:{message.from_user.id}:{uuid.uuid4().hex[:16]}"
        await state.update_data(
            wallet_reason=reason,
            pending_confirm_token=token,
            wallet_idempotency_key=idem_key,
        )
        await state.set_state(AdminSG.confirming_wallet_op)

        op_label = "افزایش سکه خریداری‌شده (+)" if op == "CREDIT" else "کسر سکه خریداری‌شده (-)"
        await message.answer(
            "⚠️ <b>تأیید نهایی عملیات کیف پول</b>\n\n"
            f"📌 نوع عملیات: <b>{op_label}</b>\n"
            f"🎯 کاربر هدف: <code>{html.escape(str(pid))}</code>\n"
            f"🪙 مقدار: <b>{amount}</b> سکه\n"
            f"📝 دلیل: {html.escape(reason)}\n\n"
            "آیا از ثبت این تراکنش در دفترکل و گزارش ممیزی اطمینان دارید؟",
            reply_markup=get_confirm_action_keyboard(f"{CB_ADM_WALLET_CONFIRM_PREFIX}{token}"),
        )
    except Exception as exc:
        logger.error("Error preparing wallet confirmation: %s", type(exc).__name__)
        await state.clear()
        await message.answer(MSG_GENERIC_ERROR)


async def handle_admin_wallet_confirm_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Execute confirmed wallet grant/debit once and reject replay clicks."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return

        token = (callback.data or "").replace(CB_ADM_WALLET_CONFIRM_PREFIX, "", 1).strip()
        data = await state.get_data()
        expected = data.get("pending_confirm_token")
        if not token or not expected or expected != token:
            await callback.answer(MSG_ADMIN_REPLAY_EXPIRED, show_alert=True)
            await state.clear()
            return

        await state.clear()
        await callback.answer("در حال ثبت تراکنش...")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        op = data.get("wallet_op")
        pid = str(data.get("wallet_target_pid", ""))
        amount = int(data.get("wallet_amount", 0))
        reason = str(data.get("wallet_reason", ""))
        idem_key = str(data.get("wallet_idempotency_key", ""))

        if op == "CREDIT":
            res = await admin_service.admin_grant_coins(
                callback.from_user.id,  # type: ignore[union-attr]
                target_public_id=pid,
                amount=amount,
                reason=reason,
                idempotency_key=idem_key,
            )
        else:
            res = await admin_service.admin_debit_coins(
                callback.from_user.id,  # type: ignore[union-attr]
                target_public_id=pid,
                amount=amount,
                reason=reason,
                idempotency_key=idem_key,
                allow_free=False,
            )

        await callback.message.answer(
            "✅ <b>عملیات کیف پول با موفقیت انجام شد.</b>\n\n"
            f"🎯 کاربر: <code>{html.escape(res.target_public_id)}</code>\n"
            f"💎 سکه خریداری‌شده فعلی: <b>{res.purchased_coins_after}</b>\n"
            f"🎁 سهمیه رایگان فعلی: <b>{res.free_coins_after}</b>\n"
            f"💰 مجموع قابل مصرف: <b>{res.total_spendable_after}</b>\n"
            f"📜 شماره ممیزی: <code>#{res.audit_log_id}</code>",
            reply_markup=get_admin_main_menu_keyboard(actor.role),
        )
    except DOMAIN_ERRORS as err:
        await state.clear()
        if isinstance(callback.message, Message):
            await callback.message.answer(f"❌ {html.escape(str(err))}")
    except Exception as exc:
        logger.error("Error confirming wallet adjustment: %s", type(exc).__name__)
        await state.clear()
        if isinstance(callback.message, Message):
            await callback.message.answer(MSG_GENERIC_ERROR)


# =============================================================================
# SUPER_ADMIN System Configurations
# =============================================================================
async def handle_admin_config_list_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Display allowlisted SystemConfiguration entries for SUPER_ADMIN."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)
    cfg_service = AdminConfigService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return
        if actor.role != "SUPER_ADMIN":
            await callback.answer("دسترسی کافی نیست.", show_alert=True)
            await callback.message.answer("⛔ تغییر تنظیمات سیستم فقط در اختیار سوپر ادمین است.")
            return

        await callback.answer()
        items = await cfg_service.list_configurations(callback.from_user.id)  # type: ignore[union-attr]
        lines = ["⚙️ <b>تنظیمات پویای سیستم (مقادیر مؤثر فعلی)</b>\n"]
        for item in items:
            lbl = html.escape(item.label_persian)
            key = html.escape(item.key)
            lines.append(
                f"• <b>{lbl}</b> (<code>{key}</code>)\n"
                f"  مقدار فعلی: <b>{item.effective_int_value}</b> "
                f"(بازه مجاز: {item.min_value} تا {item.max_value})"
            )
        lines.append("\nبرای ویرایش هر مورد روی آن کلیک کنید:")
        await callback.message.answer(
            "\n".join(lines),
            reply_markup=get_system_configs_keyboard(items),
        )
    except DOMAIN_ERRORS as err:
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.answer(f"❌ {html.escape(str(err))}")
    except Exception as exc:
        logger.error("Error listing system configs: %s", type(exc).__name__)
        await callback.answer()


async def handle_admin_config_select_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Start SUPER_ADMIN config value edit prompt."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)
    cfg_service = AdminConfigService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return
        if actor.role != "SUPER_ADMIN":
            await callback.answer("دسترسی کافی نیست.", show_alert=True)
            return

        key = (callback.data or "").replace(CB_ADM_CFG_SELECT_PREFIX, "", 1).strip()
        item = await cfg_service.get_configuration(callback.from_user.id, key)  # type: ignore[union-attr]
        await callback.answer()
        await state.clear()
        await state.set_state(AdminSG.waiting_for_config_value)
        await state.update_data(cfg_key=item.key)
        await callback.message.answer(
            f"⚙️ <b>ویرایش {html.escape(item.label_persian)}</b>\n"
            f"🔑 کلید: <code>{html.escape(item.key)}</code>\n"
            f"📊 مقدار فعلی: <b>{item.effective_int_value}</b>\n"
            f"📏 بازه مجاز: بین <b>{item.min_value}</b> تا <b>{item.max_value}</b>\n\n"
            "لطفاً مقدار عددی صحیح جدید را ارسال نمایید:",
            reply_markup=get_admin_cancel_keyboard(),
        )
    except DOMAIN_ERRORS as err:
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.answer(f"❌ {html.escape(str(err))}")
    except Exception as exc:
        logger.error("Error selecting system config: %s", type(exc).__name__)
        await callback.answer()


async def handle_admin_config_value_input(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Validate new integer value for allowlisted SystemConfiguration."""
    if message.from_user is None:
        return
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)
    cfg_service = AdminConfigService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_message(message, state, admin_service)
        if actor is None:
            return
        data = await state.get_data()
        key = str(data.get("cfg_key", ""))
        item = await cfg_service.get_configuration(message.from_user.id, key)

        try:
            val = int((message.text or "").strip())
        except ValueError:
            await message.answer(
                "❌ لطفاً یک عدد صحیح معتبر ارسال کنید:",
                reply_markup=get_admin_cancel_keyboard(),
            )
            return

        if val < item.min_value or val > item.max_value:
            await message.answer(
                f"❌ مقدار باید بین {item.min_value} تا {item.max_value} باشد:",
                reply_markup=get_admin_cancel_keyboard(),
            )
            return

        await state.update_data(cfg_new_value=val)
        await state.set_state(AdminSG.waiting_for_config_reason)
        await message.answer(
            "📝 لطفاً دلیل تغییر این تنظیم سیستمی را وارد نمایید (در گزارش ممیزی ثبت می‌شود):",
            reply_markup=get_admin_cancel_keyboard(),
        )
    except DOMAIN_ERRORS as err:
        await state.clear()
        await message.answer(f"❌ {html.escape(str(err))}")
    except Exception as exc:
        logger.error("Error validating config value: %s", type(exc).__name__)
        await state.clear()
        await message.answer(MSG_GENERIC_ERROR)


async def handle_admin_config_reason_input(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Present two-step confirmation card for SystemConfiguration change."""
    if message.from_user is None:
        return
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_message(message, state, admin_service)
        if actor is None:
            return
        reason = (message.text or "").strip()
        if not reason:
            await message.answer(
                "❌ ذکر دلیل الزامی است:",
                reply_markup=get_admin_cancel_keyboard(),
            )
            return

        data = await state.get_data()
        key = data.get("cfg_key")
        val = data.get("cfg_new_value")
        if not key or val is None:
            await state.clear()
            await message.answer("❌ اطلاعات تغییر تنظیمات ناقص است.")
            return

        token = secrets.token_hex(6)
        await state.update_data(cfg_reason=reason, pending_confirm_token=token)
        await state.set_state(AdminSG.confirming_config_update)
        await message.answer(
            "⚠️ <b>تأیید نهایی تغییر تنظیمات سیستم</b>\n\n"
            f"🔑 کلید: <code>{html.escape(str(key))}</code>\n"
            f"🔢 مقدار جدید: <b>{val}</b>\n"
            f"📝 دلیل: {html.escape(reason)}\n\n"
            "آیا تغییر در پایگاه داده اعمال شود؟",
            reply_markup=get_confirm_action_keyboard(f"{CB_ADM_CFG_CONFIRM_PREFIX}{token}"),
        )
    except Exception as exc:
        logger.error("Error preparing config confirmation: %s", type(exc).__name__)
        await state.clear()
        await message.answer(MSG_GENERIC_ERROR)


async def handle_admin_config_confirm_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Commit confirmed system config change and invalidate replay token."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)
    cfg_service = AdminConfigService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return

        token = (callback.data or "").replace(CB_ADM_CFG_CONFIRM_PREFIX, "", 1).strip()
        data = await state.get_data()
        expected = data.get("pending_confirm_token")
        if not token or not expected or expected != token:
            await callback.answer(MSG_ADMIN_REPLAY_EXPIRED, show_alert=True)
            await state.clear()
            return

        await state.clear()
        await callback.answer("در حال ذخیره تنظیمات...")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        dto, audit_id = await cfg_service.update_configuration(
            callback.from_user.id,  # type: ignore[union-attr]
            key=str(data.get("cfg_key", "")),
            value=int(data.get("cfg_new_value")),
            reason=str(data.get("cfg_reason", "")),
        )
        await callback.message.answer(
            "✅ <b>تنظیمات سیستم با موفقیت به‌روزرسانی شد.</b>\n\n"
            f"🔑 کلید: <code>{html.escape(dto.key)}</code>\n"
            f"🔢 مقدار مؤثر جدید: <b>{dto.effective_int_value}</b>\n"
            f"📜 شماره ممیزی: <code>#{audit_id}</code>",
            reply_markup=get_admin_main_menu_keyboard(actor.role),
        )
    except DOMAIN_ERRORS as err:
        await state.clear()
        if isinstance(callback.message, Message):
            await callback.message.answer(f"❌ {html.escape(str(err))}")
    except Exception as exc:
        logger.error("Error confirming system config update: %s", type(exc).__name__)
        await state.clear()
        if isinstance(callback.message, Message):
            await callback.message.answer(MSG_GENERIC_ERROR)


# =============================================================================
# SUPER_ADMIN Audit Logs & Role Management
# =============================================================================
async def handle_admin_audit_logs_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """List paginated append-only audit logs for SUPER_ADMIN."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return
        if actor.role != "SUPER_ADMIN":
            await callback.answer("دسترسی کافی نیست.", show_alert=True)
            await callback.message.answer("⛔ مشاهده گزارش ممیزی فقط در اختیار سوپر ادمین است.")
            return

        raw_offset = (callback.data or "").replace(CB_ADM_AUDIT_PREFIX, "", 1).strip()
        try:
            offset = max(0, int(raw_offset))
        except ValueError:
            offset = 0

        await callback.answer()
        page = await admin_service.list_audit_logs(
            callback.from_user.id,  # type: ignore[union-attr]
            offset=offset,
            limit=ADMIN_PAGE_SIZE,
        )
        await callback.message.answer(
            format_audit_logs_page(page, resolved_settings),
            reply_markup=get_audit_logs_keyboard(page),
        )
    except DOMAIN_ERRORS as err:
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.answer(f"❌ {html.escape(str(err))}")
    except Exception as exc:
        logger.error("Error listing audit logs: %s", type(exc).__name__)
        await callback.answer()


async def handle_admin_roles_list_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """List all AdminUser role assignments for SUPER_ADMIN."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return
        if actor.role != "SUPER_ADMIN":
            await callback.answer("دسترسی کافی نیست.", show_alert=True)
            await callback.message.answer("⛔ مدیریت نقش‌های ادمین فقط در اختیار سوپر ادمین است.")
            return

        await callback.answer()
        roles = await admin_service.list_admin_roles(callback.from_user.id)  # type: ignore[union-attr]
        lines = ["🛡 <b>فهرست نقش‌های مدیریتی سامانه</b>\n"]
        if not roles:
            lines.append("هیچ نقشی ثبت نشده است.")
        else:
            for item in roles:
                st = "✅ فعال" if item.is_active else "🚫 غیرفعال"
                lines.append(
                    f"• <code>{html.escape(item.public_id)}</code> | "
                    f"<b>{html.escape(item.role)}</b> | {st}"
                )
        await callback.message.answer(
            "\n".join(lines),
            reply_markup=get_roles_management_keyboard(roles),
        )
    except DOMAIN_ERRORS as err:
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.answer(f"❌ {html.escape(str(err))}")
    except Exception as exc:
        logger.error("Error listing admin roles: %s", type(exc).__name__)
        await callback.answer()


async def handle_admin_role_add_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Prompt SUPER_ADMIN to pick a role to assign."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return
        if actor.role != "SUPER_ADMIN":
            await callback.answer("دسترسی کافی نیست.", show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await callback.message.answer(
            "🛡 <b>انتخاب نقش مدیریتی</b>\n\nنقش مورد نظر جهت اعطا یا تغییر را انتخاب نمایید:",
            reply_markup=get_role_pick_keyboard(),
        )
    except Exception as exc:
        logger.error("Error starting role assignment: %s", type(exc).__name__)
        await callback.answer()


async def handle_admin_role_pick_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Store selected role and prompt for target user's public_id."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return
        if actor.role != "SUPER_ADMIN":
            await callback.answer("دسترسی کافی نیست.", show_alert=True)
            return
        picked_role = (callback.data or "").replace(CB_ADM_ROLE_PICK_PREFIX, "", 1).strip().upper()
        if picked_role not in VALID_ADMIN_ROLES:
            await callback.answer("نقش نامعتبر است.", show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await state.set_state(AdminSG.waiting_for_role_public_id)
        await state.update_data(role_op="ASSIGN", role_name=picked_role)
        await callback.message.answer(
            f"🛡 اعطای نقش <b>{html.escape(picked_role)}</b>\n\n"
            "لطفاً شناسه عمومی کاربر هدف (مثلاً <code>usr_xxxxxxxxxxxx</code>) را ارسال کنید:",
            reply_markup=get_admin_cancel_keyboard(),
        )
    except Exception as exc:
        logger.error("Error picking role: %s", type(exc).__name__)
        await callback.answer()


async def handle_admin_role_revoke_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Start role revocation flow for an operator public_id."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return
        if actor.role != "SUPER_ADMIN":
            await callback.answer("دسترسی کافی نیست.", show_alert=True)
            return
        pid = (callback.data or "").replace(CB_ADM_ROLE_REVOKE_PREFIX, "", 1).strip()
        if not pid:
            await callback.answer("شناسه نامعتبر است.", show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await state.set_state(AdminSG.waiting_for_role_reason)
        await state.update_data(role_op="REVOKE", role_target_pid=pid, role_name="REVOKE")
        await callback.message.answer(
            f"🚫 لغو دسترسی مدیریتی کاربر <code>{html.escape(pid)}</code>\n\n"
            "لطفاً دلیل لغو دسترسی را وارد کنید (الزامی):",
            reply_markup=get_admin_cancel_keyboard(),
        )
    except Exception as exc:
        logger.error("Error initiating role revoke: %s", type(exc).__name__)
        await callback.answer()


async def handle_admin_role_public_id_input(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Save target public_id for role assignment and request mandatory reason."""
    if message.from_user is None:
        return
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_message(message, state, admin_service)
        if actor is None:
            return
        pid = (message.text or "").strip()
        if not pid:
            await message.answer(
                "❌ شناسه عمومی نمی‌تواند خالی باشد:",
                reply_markup=get_admin_cancel_keyboard(),
            )
            return

        await state.update_data(role_target_pid=pid)
        await state.set_state(AdminSG.waiting_for_role_reason)
        await message.answer(
            "📝 لطفاً دلیل اعطا یا تغییر این نقش مدیریتی را وارد کنید (الزامی):",
            reply_markup=get_admin_cancel_keyboard(),
        )
    except Exception as exc:
        logger.error("Error reading role target public_id: %s", type(exc).__name__)
        await state.clear()
        await message.answer(MSG_GENERIC_ERROR)


async def handle_admin_role_reason_input(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Show two-step confirmation card before applying role change."""
    if message.from_user is None:
        return
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_message(message, state, admin_service)
        if actor is None:
            return
        reason = (message.text or "").strip()
        if not reason:
            await message.answer(
                "❌ ذکر دلیل الزامی است:",
                reply_markup=get_admin_cancel_keyboard(),
            )
            return

        data = await state.get_data()
        op = data.get("role_op")
        pid = data.get("role_target_pid")
        role_name = data.get("role_name")
        if not op or not pid:
            await state.clear()
            await message.answer("❌ اطلاعات تغییر نقش ناقص است.")
            return

        token = secrets.token_hex(6)
        await state.update_data(role_reason=reason, pending_confirm_token=token)
        await state.set_state(AdminSG.confirming_role_change)

        desc = (
            f"اعطا/تغییر نقش به <b>{html.escape(str(role_name))}</b>"
            if op == "ASSIGN"
            else "لغو دسترسی مدیریتی"
        )
        await message.answer(
            "⚠️ <b>تأیید نهایی تغییر نقش ادمین</b>\n\n"
            f"📌 عملیات: {desc}\n"
            f"🎯 کاربر هدف: <code>{html.escape(str(pid))}</code>\n"
            f"📝 دلیل: {html.escape(reason)}\n\n"
            "آیا این تغییر اعمال شود؟",
            reply_markup=get_confirm_action_keyboard(f"{CB_ADM_ROLE_CONFIRM_PREFIX}{token}"),
        )
    except Exception as exc:
        logger.error("Error preparing role confirmation: %s", type(exc).__name__)
        await state.clear()
        await message.answer(MSG_GENERIC_ERROR)


async def handle_admin_role_confirm_callback(
    callback: CallbackQuery,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Commit confirmed role assignment/revocation once."""
    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)
    admin_service = AdminService(resolved_db, resolved_settings)

    try:
        actor = await _get_admin_or_deny_callback(callback, state, admin_service)
        if actor is None or not isinstance(callback.message, Message):
            return

        token = (callback.data or "").replace(CB_ADM_ROLE_CONFIRM_PREFIX, "", 1).strip()
        data = await state.get_data()
        expected = data.get("pending_confirm_token")
        if not token or not expected or expected != token:
            await callback.answer(MSG_ADMIN_REPLAY_EXPIRED, show_alert=True)
            await state.clear()
            return

        await state.clear()
        await callback.answer("در حال اعمال تغییر نقش...")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        op = data.get("role_op")
        pid = str(data.get("role_target_pid", ""))
        reason = str(data.get("role_reason", ""))

        if op == "ASSIGN":
            dto, audit_id = await admin_service.assign_or_update_admin_role(
                callback.from_user.id,  # type: ignore[union-attr]
                target_public_id=pid,
                role=str(data.get("role_name", "")),
                reason=reason,
            )
            status_txt = f"نقش <b>{html.escape(dto.role)}</b> فعال شد"
        else:
            dto, audit_id = await admin_service.revoke_admin_role(
                callback.from_user.id,  # type: ignore[union-attr]
                target_public_id=pid,
                reason=reason,
            )
            status_txt = "دسترسی مدیریتی لغو شد"

        await callback.message.answer(
            f"✅ <b>عملیات نقش مدیریتی ثبت شد.</b>\n\n"
            f"🎯 کاربر: <code>{html.escape(dto.public_id)}</code>\n"
            f"📌 نتیجه: {status_txt}\n"
            f"📜 شماره ممیزی: <code>#{audit_id}</code>",
            reply_markup=get_admin_main_menu_keyboard(actor.role),
        )
    except DOMAIN_ERRORS as err:
        await state.clear()
        if isinstance(callback.message, Message):
            await callback.message.answer(f"❌ {html.escape(str(err))}")
    except Exception as exc:
        logger.error("Error confirming role change: %s", type(exc).__name__)
        await state.clear()
        if isinstance(callback.message, Message):
            await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_pending_confirm_text_reminder(message: Message) -> None:
    """Prevent accidental relay if admin sends text while on a confirmation step."""
    await message.answer(
        "لطفاً از دکمه‌های «✅ تأیید نهایی و ثبت» یا «❌ انصراف» در پیام قبلی استفاده نمایید."
    )


def get_admin_router() -> Router:
    """Create and configure the Telegram administration & moderation router."""
    router = Router(name="admin")

    # /admin entry & navigation
    router.message.register(handle_admin_command, Command("admin"))
    router.callback_query.register(handle_admin_home_callback, F.data == CB_ADM_HOME)
    router.callback_query.register(handle_admin_cancel_callback, F.data == CB_ADM_CANCEL)

    # Reports queue, inspect & resolve
    router.callback_query.register(
        handle_admin_report_queue_callback, F.data.startswith(CB_ADM_REP_QUEUE_PREFIX)
    )
    router.callback_query.register(
        handle_admin_report_open_callback, F.data.startswith(CB_ADM_REP_OPEN_PREFIX)
    )
    router.callback_query.register(
        handle_admin_report_resolve_callback, F.data.startswith(CB_ADM_REP_RESOLVE_PREFIX)
    )
    router.message.register(handle_admin_report_notes_input, AdminSG.waiting_for_report_notes)

    # User lookup & inspection
    router.callback_query.register(handle_admin_user_lookup_prompt, F.data == CB_ADM_USER_LOOKUP)
    router.message.register(handle_admin_user_lookup_input, AdminSG.waiting_for_user_public_id)
    router.callback_query.register(
        handle_admin_user_view_callback, F.data.startswith(CB_ADM_USER_VIEW_PREFIX)
    )

    # Moderation actions & restore
    router.callback_query.register(
        handle_admin_user_moderation_callback, F.data.startswith(CB_ADM_MOD_PREFIX)
    )
    router.callback_query.register(
        handle_admin_report_moderation_callback, F.data.startswith(CB_ADM_REP_MOD_PREFIX)
    )
    router.message.register(
        handle_admin_moderation_duration_input, AdminSG.waiting_for_moderation_duration
    )
    router.message.register(
        handle_admin_moderation_reason_input, AdminSG.waiting_for_moderation_reason
    )

    # Moderator user-pair block & unblock
    router.callback_query.register(handle_admin_pair_menu_callback, F.data == CB_ADM_PAIR_MENU)
    router.callback_query.register(
        handle_admin_pair_block_start_callback, F.data == CB_ADM_PAIR_BLK
    )
    router.callback_query.register(
        handle_admin_report_pair_block_callback, F.data.startswith(CB_ADM_REP_PAIRBLK_PREFIX)
    )
    router.message.register(handle_admin_pair_block_input, AdminSG.waiting_for_pair_block_input)
    router.callback_query.register(
        handle_admin_pair_unblock_start_callback, F.data == CB_ADM_PAIR_UNBLK
    )
    router.message.register(handle_admin_pair_unblock_input, AdminSG.waiting_for_pair_unblock_input)

    # SUPER_ADMIN wallet admin
    router.callback_query.register(handle_admin_wallet_menu_callback, F.data == CB_ADM_WALLET_MENU)
    router.message.register(
        handle_admin_wallet_public_id_input, AdminSG.waiting_for_wallet_public_id
    )
    router.callback_query.register(
        handle_admin_wallet_open_or_ledger_callback,
        F.data.startswith(CB_ADM_WALLET_OPEN_PREFIX)
        | F.data.startswith(CB_ADM_WALLET_LEDGER_PREFIX),
    )
    router.callback_query.register(
        handle_admin_wallet_adjust_start_callback,
        F.data.startswith(CB_ADM_WALLET_CREDIT_PREFIX)
        | F.data.startswith(CB_ADM_WALLET_DEBIT_PREFIX),
    )
    router.message.register(handle_admin_wallet_amount_input, AdminSG.waiting_for_wallet_amount)
    router.message.register(handle_admin_wallet_reason_input, AdminSG.waiting_for_wallet_reason)
    router.callback_query.register(
        handle_admin_wallet_confirm_callback,
        AdminSG.confirming_wallet_op,
        F.data.startswith(CB_ADM_WALLET_CONFIRM_PREFIX),
    )
    # Catch-all replay/expired confirm clicks even after state cleared
    router.callback_query.register(
        handle_admin_wallet_confirm_callback,
        F.data.startswith(CB_ADM_WALLET_CONFIRM_PREFIX),
    )

    # SUPER_ADMIN system configs
    router.callback_query.register(handle_admin_config_list_callback, F.data == CB_ADM_CFG_LIST)
    router.callback_query.register(
        handle_admin_config_select_callback, F.data.startswith(CB_ADM_CFG_SELECT_PREFIX)
    )
    router.message.register(handle_admin_config_value_input, AdminSG.waiting_for_config_value)
    router.message.register(handle_admin_config_reason_input, AdminSG.waiting_for_config_reason)
    router.callback_query.register(
        handle_admin_config_confirm_callback,
        AdminSG.confirming_config_update,
        F.data.startswith(CB_ADM_CFG_CONFIRM_PREFIX),
    )
    router.callback_query.register(
        handle_admin_config_confirm_callback,
        F.data.startswith(CB_ADM_CFG_CONFIRM_PREFIX),
    )

    # SUPER_ADMIN audit logs & roles
    router.callback_query.register(
        handle_admin_audit_logs_callback, F.data.startswith(CB_ADM_AUDIT_PREFIX)
    )
    router.callback_query.register(handle_admin_roles_list_callback, F.data == CB_ADM_ROLES)
    router.callback_query.register(handle_admin_role_add_callback, F.data == CB_ADM_ROLE_ADD)
    router.callback_query.register(
        handle_admin_role_pick_callback, F.data.startswith(CB_ADM_ROLE_PICK_PREFIX)
    )
    router.callback_query.register(
        handle_admin_role_revoke_callback, F.data.startswith(CB_ADM_ROLE_REVOKE_PREFIX)
    )
    router.message.register(handle_admin_role_public_id_input, AdminSG.waiting_for_role_public_id)
    router.message.register(handle_admin_role_reason_input, AdminSG.waiting_for_role_reason)
    router.callback_query.register(
        handle_admin_role_confirm_callback,
        AdminSG.confirming_role_change,
        F.data.startswith(CB_ADM_ROLE_CONFIRM_PREFIX),
    )
    router.callback_query.register(
        handle_admin_role_confirm_callback,
        F.data.startswith(CB_ADM_ROLE_CONFIRM_PREFIX),
    )

    # Reminder if admin sends text while waiting on inline confirmation
    router.message.register(handle_pending_confirm_text_reminder, AdminSG.confirming_wallet_op)
    router.message.register(handle_pending_confirm_text_reminder, AdminSG.confirming_config_update)
    router.message.register(handle_pending_confirm_text_reminder, AdminSG.confirming_role_change)

    return router
