"""Inline keyboards for user abuse reporting flow."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.app.modules.moderation.schemas import REPORT_CATEGORIES

# Callback prefixes and actions
CB_REPORT_START_CHAT_PREFIX = "rep:chat:"
CB_REPORT_START_USER_PREFIX = "rep:usr:"
CB_REPORT_CAT_PREFIX = "rep:cat:"
CB_REPORT_SKIP_DESC = "rep:skipdesc"
CB_REPORT_CANCEL = "rep:cancel"


def get_report_category_keyboard() -> InlineKeyboardMarkup:
    """Build inline keyboard for selecting one of the allowed report categories."""
    rows: list[list[InlineKeyboardButton]] = []
    items = list(REPORT_CATEGORIES.items())
    for idx in range(0, len(items), 2):
        pair = items[idx : idx + 2]
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"{CB_REPORT_CAT_PREFIX}{code}",
                )
                for code, label in pair
            ]
        )
    rows.append([InlineKeyboardButton(text="❌ انصراف", callback_data=CB_REPORT_CANCEL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_report_description_keyboard() -> InlineKeyboardMarkup:
    """Inline keyboard for optional report description step."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏭ ثبت گزارش بدون توضیحات",
                    callback_data=CB_REPORT_SKIP_DESC,
                )
            ],
            [InlineKeyboardButton(text="❌ انصراف", callback_data=CB_REPORT_CANCEL)],
        ]
    )
