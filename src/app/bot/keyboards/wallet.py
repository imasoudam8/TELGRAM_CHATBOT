"""Inline keyboards for Persian wallet view and paginated transaction history."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

CB_WALLET_REFRESH = "wallet:refresh"
CB_WALLET_HISTORY_PREFIX = "wallet:hist:"


def get_wallet_menu_keyboard() -> InlineKeyboardMarkup:
    """Create inline actions for wallet refresh and transaction history."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📜 تاریخچه تراکنش‌ها",
                    callback_data=f"{CB_WALLET_HISTORY_PREFIX}0",
                ),
                InlineKeyboardButton(
                    text="🔄 بروزرسانی موجودی",
                    callback_data=CB_WALLET_REFRESH,
                ),
            ]
        ]
    )


def get_wallet_history_keyboard(
    *,
    offset: int,
    limit: int,
    has_prev: bool,
    has_next: bool,
) -> InlineKeyboardMarkup:
    """Create pagination keyboard for wallet ledger history."""
    nav_row: list[InlineKeyboardButton] = []
    if has_prev:
        prev_offset = max(0, offset - limit)
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ قبلی",
                callback_data=f"{CB_WALLET_HISTORY_PREFIX}{prev_offset}",
            )
        )
    if has_next:
        nav_row.append(
            InlineKeyboardButton(
                text="بعدی ➡️",
                callback_data=f"{CB_WALLET_HISTORY_PREFIX}{offset + limit}",
            )
        )

    rows: list[list[InlineKeyboardButton]] = []
    if nav_row:
        rows.append(nav_row)
    rows.append(
        [InlineKeyboardButton(text="🪙 بازگشت به کیف پول", callback_data=CB_WALLET_REFRESH)]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
