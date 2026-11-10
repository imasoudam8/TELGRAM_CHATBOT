"""Inline keyboards and UI builders for user profile creation, view, and editing."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Gender choices and Persian display labels
GENDER_LABELS = {
    "MALE": "👨 آقا (مرد)",
    "FEMALE": "👩 خانم (زن)",
    "NON_BINARY": "⚧ غیردوگانه (Non-binary)",
    "OTHER": "✨ سایر",
}

# Callback tokens for wizard
CB_GENDER_PREFIX = "prof:gender:"
CB_SKIP_CITY = "prof:skip_city"
CB_SKIP_BIO = "prof:skip_bio"
CB_CONFIRM_CREATE = "prof:confirm_create"
CB_CANCEL_WIZARD = "prof:cancel_wizard"
CB_START_CREATE = "prof:start_create"

# Callback tokens for editing existing profile
CB_EDIT_PREFIX = "prof:edit:"
CB_EDIT_NAME = "prof:edit:display_name"
CB_EDIT_AGE = "prof:edit:age"
CB_EDIT_GENDER = "prof:edit:gender"
CB_EDIT_CITY = "prof:edit:city"
CB_EDIT_BIO = "prof:edit:bio"
CB_BACK_TO_PROFILE = "prof:view"
CB_BACK_TO_MAIN_MENU = "prof:main_menu"


def get_gender_selection_keyboard(is_editing: bool = False) -> InlineKeyboardMarkup:
    """Keyboard for selecting gender during creation or editing."""
    prefix = f"{CB_GENDER_PREFIX}edit:" if is_editing else CB_GENDER_PREFIX
    keyboard = [
        [
            InlineKeyboardButton(text="👨 مرد", callback_data=f"{prefix}MALE"),
            InlineKeyboardButton(text="👩 زن", callback_data=f"{prefix}FEMALE"),
        ],
        [
            InlineKeyboardButton(text="⚧ غیردوگانه", callback_data=f"{prefix}NON_BINARY"),
            InlineKeyboardButton(text="✨ سایر", callback_data=f"{prefix}OTHER"),
        ],
    ]
    if is_editing:
        keyboard.append([InlineKeyboardButton(text="🔙 انصراف", callback_data=CB_BACK_TO_PROFILE)])
    else:
        keyboard.append(
            [InlineKeyboardButton(text="❌ انصراف از ثبت‌نام", callback_data=CB_CANCEL_WIZARD)]
        )
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_skip_keyboard(
    skip_callback: str, cancel_callback: str = CB_CANCEL_WIZARD
) -> InlineKeyboardMarkup:
    """Keyboard with a 'Skip' button and a 'Cancel' button."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏩ رد کردن این مرحله", callback_data=skip_callback),
            ],
            [
                InlineKeyboardButton(text="❌ انصراف", callback_data=cancel_callback),
            ],
        ]
    )


def get_review_keyboard() -> InlineKeyboardMarkup:
    """Keyboard to confirm or discard entered profile data."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ تأیید و ثبت نهایی", callback_data=CB_CONFIRM_CREATE),
            ],
            [
                InlineKeyboardButton(text="❌ انصراف و شروع مجدد", callback_data=CB_CANCEL_WIZARD),
            ],
        ]
    )


def get_no_profile_keyboard() -> InlineKeyboardMarkup:
    """Prompt keyboard when user has no profile."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✨ ایجاد پروفایل من", callback_data=CB_START_CREATE),
            ],
        ]
    )


def get_view_profile_keyboard() -> InlineKeyboardMarkup:
    """Keyboard on existing profile display with edit options."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ ویرایش نام", callback_data=CB_EDIT_NAME),
                InlineKeyboardButton(text="✏️ ویرایش سن", callback_data=CB_EDIT_AGE),
            ],
            [
                InlineKeyboardButton(text="✏️ ویرایش جنسیت", callback_data=CB_EDIT_GENDER),
                InlineKeyboardButton(text="✏️ ویرایش شهر", callback_data=CB_EDIT_CITY),
            ],
            [
                InlineKeyboardButton(text="✏️ ویرایش درباره من", callback_data=CB_EDIT_BIO),
            ],
        ]
    )


def get_cancel_edit_keyboard() -> InlineKeyboardMarkup:
    """Keyboard allowing user to cancel field editing."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔙 انصراف از ویرایش", callback_data=CB_BACK_TO_PROFILE),
            ],
        ]
    )
