"""Keyboards for onboarding, adult self-declaration, and main navigation menu."""

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

# Inline button labels for age self-declaration
AGE_CONFIRM_TEXT = "✅ تأیید میکنم، ۱۸ سال یا بیشتر دارم"
AGE_DECLINE_TEXT = "❌ زیر ۱۸ سال هستم"

# Callback query data tokens
CALLBACK_AGE_CONFIRM = "onboarding:age_confirm"
CALLBACK_AGE_DECLINE = "onboarding:age_decline"

# Persian main menu button labels (exact strings required by specification)
BTN_RANDOM_CHAT = "🎲 گفتگوی تصادفی"
BTN_VIEW_PROFILES = "🔎 مشاهده پروفایلها"
BTN_DIRECT_REQUEST = "✉️ درخواست مستقیم"
BTN_MY_CONNECTIONS = "👥 ارتباطهای من"
BTN_MY_PROFILE = "👤 پروفایل من"
BTN_WALLET = "🪙 کیف پول"
BTN_HELP_SUPPORT = "🆘 راهنما و پشتیبانی"

MAIN_MENU_BUTTONS: list[str] = [
    BTN_RANDOM_CHAT,
    BTN_VIEW_PROFILES,
    BTN_DIRECT_REQUEST,
    BTN_MY_CONNECTIONS,
    BTN_MY_PROFILE,
    BTN_WALLET,
    BTN_HELP_SUPPORT,
]


def get_age_declaration_keyboard() -> InlineKeyboardMarkup:
    """Create the inline keyboard prompting users to confirm they are 18+."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=AGE_CONFIRM_TEXT,
                    callback_data=CALLBACK_AGE_CONFIRM,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=AGE_DECLINE_TEXT,
                    callback_data=CALLBACK_AGE_DECLINE,
                ),
            ],
        ]
    )


def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Create the persistent Persian main menu reply keyboard."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=BTN_RANDOM_CHAT),
                KeyboardButton(text=BTN_VIEW_PROFILES),
            ],
            [
                KeyboardButton(text=BTN_DIRECT_REQUEST),
                KeyboardButton(text=BTN_MY_CONNECTIONS),
            ],
            [
                KeyboardButton(text=BTN_MY_PROFILE),
                KeyboardButton(text=BTN_WALLET),
            ],
            [
                KeyboardButton(text=BTN_HELP_SUPPORT),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )
