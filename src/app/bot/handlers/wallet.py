"""Telegram handlers for viewing wallet balance, daily free allowance, and ledger history."""

import html
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.app.bot.handlers.common import (
    MSG_GENERIC_ERROR,
    load_authorized_user,
    reply_with_auth_error,
)
from src.app.bot.keyboards.onboarding import BTN_WALLET
from src.app.bot.keyboards.wallet import (
    CB_WALLET_HISTORY_PREFIX,
    CB_WALLET_REFRESH,
    get_wallet_history_keyboard,
    get_wallet_menu_keyboard,
)
from src.app.config.settings import Settings, get_settings
from src.app.infrastructure.database.base import to_business_tz
from src.app.infrastructure.database.session import DatabaseManager, get_database_manager
from src.app.modules.wallet.schemas import LedgerEntryDTO, WalletBalanceDTO
from src.app.modules.wallet.service import WalletService
from src.app.modules.wallet.settlement import (
    DIRECT_CHAT_COST_COINS,
    RANDOM_CHAT_COST_COINS,
    ChatSettlementService,
)

logger = logging.getLogger(__name__)

HISTORY_PAGE_SIZE = 5

TX_TYPE_LABELS: dict[str, str] = {
    "DAILY_GRANT": "🎁 سهمیه رایگان روزانه",
    "PURCHASED_CREDIT": "➕ افزایش موجودی خریداری‌شده",
    "PURCHASED_DEBIT": "➖ کسر از موجودی",
    "ADMIN_GRANT": "🛠 افزایش توسط پشتیبانی",
    "ADMIN_DEBIT": "🛠 کسر توسط پشتیبانی",
    "CHAT_SETTLEMENT": "💬 تسویه گفتگوی ناشناس",
}

CREDIT_TYPE_LABELS: dict[str, str] = {
    "FREE": "رایگان روزانه",
    "PURCHASED": "خریداری‌شده",
}


def format_wallet_summary(
    balance: WalletBalanceDTO,
    settings: Settings,
    *,
    random_cost: int = RANDOM_CHAT_COST_COINS,
    direct_cost: int = DIRECT_CHAT_COST_COINS,
) -> str:
    """Format Persian HTML wallet balance overview cleanly without exposing sensitive IDs."""
    expiry_note = "فعال نیست (سهمیه امروز مصرف شده یا هنوز اختصاص نیافته است)"
    if balance.free_expires_at is not None:
        local_exp = to_business_tz(balance.free_expires_at, settings.business_timezone)
        expiry_note = f"تا ساعت ۰۰:۰۰ بامداد ({local_exp.strftime('%Y-%m-%d')} به وقت تهران)"

    return (
        "🪙 <b>کیف پول سکه‌های چت شما</b>\n\n"
        f"💎 سکه‌های خریداری‌شده (بدون انقضا): <b>{balance.purchased_coins}</b>\n"
        f"🎁 سهمیه رایگان امروز: <b>{balance.free_coins}</b>\n"
        f"💰 مجموع موجودی قابل مصرف: <b>{balance.total_spendable_coins}</b>\n"
        f"⏳ اعتبار سهمیه رایگان: {html.escape(expiry_note)}\n\n"
        "📌 <b>تعرفه گفتگوها:</b>\n"
        f"• گفتگوی تصادفی: {random_cost} سکه برای هر نفر "
        "(پس از ارسال پیام توسط هر دو طرف)\n"
        f"• درخواست مستقیم: {direct_cost} سکه فقط برای درخواست‌دهنده "
        "(پس از ارسال پیام توسط هر دو طرف)\n"
        "• اولویت مصرف همیشه با سهمیه رایگان روزانه است."
    )


def format_history_page(entries: tuple[LedgerEntryDTO, ...], total_count: int) -> str:
    """Format paginated immutable wallet ledger entries in Persian HTML."""
    if not entries:
        return "📜 <b>تاریخچه تراکنش‌های کیف پول</b>\n\nهنوز هیچ تراکنشی ثبت نشده است."

    lines = [f"📜 <b>تاریخچه تراکنش‌های کیف پول ({total_count} مورد)</b>\n"]
    for item in entries:
        tx_label = TX_TYPE_LABELS.get(item.transaction_type, html.escape(item.transaction_type))
        credit_label = CREDIT_TYPE_LABELS.get(item.credit_type, html.escape(item.credit_type))
        total_before = item.purchased_balance_before + item.free_balance_before
        total_after = item.purchased_balance_after + item.free_balance_after
        sign = "+" if total_after >= total_before else "-"
        lines.append(
            f"• {tx_label} ({credit_label}): <b>{sign}{item.amount}</b>\n"
            f"  موجودی: {total_before} ⬅️ {total_after}\n"
            f"  دلیل: {html.escape(item.reason)}"
        )
    return "\n".join(lines)


async def handle_wallet_menu_entry(
    message: Message,
    state: FSMContext,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Handle '🪙 کیف پول' main menu button."""
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

        balance = await WalletService(resolved_db, resolved_settings).get_wallet_balance(
            user.id, auto_allocate_daily=True
        )
        random_cost, direct_cost = await ChatSettlementService(
            resolved_db, resolved_settings
        ).get_effective_tariffs()
        await message.answer(
            format_wallet_summary(
                balance,
                resolved_settings,
                random_cost=random_cost,
                direct_cost=direct_cost,
            ),
            reply_markup=get_wallet_menu_keyboard(),
        )
    except Exception as exc:
        logger.error("Error opening wallet view: %s", type(exc).__name__)
        await message.answer(MSG_GENERIC_ERROR)


async def handle_wallet_refresh_callback(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Refresh wallet overview and ensure daily allowance allocation."""
    await callback.answer("موجودی بروزرسانی شد.")
    if callback.from_user is None or not isinstance(callback.message, Message):
        return

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(resolved_db, callback.from_user.id)
        if err is not None or user is None:
            await reply_with_auth_error(callback, err or MSG_GENERIC_ERROR)
            return

        balance = await WalletService(resolved_db, resolved_settings).get_wallet_balance(
            user.id, auto_allocate_daily=True
        )
        random_cost, direct_cost = await ChatSettlementService(
            resolved_db, resolved_settings
        ).get_effective_tariffs()
        await callback.message.answer(
            format_wallet_summary(
                balance,
                resolved_settings,
                random_cost=random_cost,
                direct_cost=direct_cost,
            ),
            reply_markup=get_wallet_menu_keyboard(),
        )
    except Exception as exc:
        logger.error("Error refreshing wallet view: %s", type(exc).__name__)
        await callback.message.answer(MSG_GENERIC_ERROR)


async def handle_wallet_history_callback(
    callback: CallbackQuery,
    db_manager: DatabaseManager | None = None,
    settings: Settings | None = None,
) -> None:
    """Show paginated transaction history from immutable wallet ledger."""
    await callback.answer()
    if callback.from_user is None or not isinstance(callback.message, Message):
        return

    raw_offset = (callback.data or "").replace(CB_WALLET_HISTORY_PREFIX, "").strip()
    try:
        offset = max(0, int(raw_offset))
    except ValueError:
        offset = 0

    resolved_settings = settings or get_settings()
    resolved_db = db_manager or get_database_manager(resolved_settings)

    try:
        user, err = await load_authorized_user(resolved_db, callback.from_user.id)
        if err is not None or user is None:
            await reply_with_auth_error(callback, err or MSG_GENERIC_ERROR)
            return

        page = await WalletService(resolved_db, resolved_settings).get_transaction_history(
            user.id, offset=offset, limit=HISTORY_PAGE_SIZE
        )
        await callback.message.answer(
            format_history_page(page.items, page.total_count),
            reply_markup=get_wallet_history_keyboard(
                offset=page.offset,
                limit=page.limit,
                has_prev=page.has_prev,
                has_next=page.has_next,
            ),
        )
    except Exception as exc:
        logger.error("Error showing wallet history: %s", type(exc).__name__)
        await callback.message.answer(MSG_GENERIC_ERROR)


def get_wallet_router() -> Router:
    """Create and configure the wallet router."""
    router = Router(name="wallet")

    router.message.register(handle_wallet_menu_entry, F.text == BTN_WALLET)
    router.callback_query.register(handle_wallet_refresh_callback, F.data == CB_WALLET_REFRESH)
    router.callback_query.register(
        handle_wallet_history_callback, F.data.startswith(CB_WALLET_HISTORY_PREFIX)
    )

    return router
