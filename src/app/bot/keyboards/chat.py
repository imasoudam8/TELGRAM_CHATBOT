"""Keyboards for direct requests, random matching, active chats, and connections."""

import uuid

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from src.app.modules.chat.connections import ConnectionItemDTO

# Active chat reply keyboard buttons
BTN_END_CHAT = "❌ پایان گفتگو"
BTN_CHAT_BLOCK = "🚫 مسدود کردن مخاطب"

# Matching callbacks
CB_MATCH_JOIN = "match:join"
CB_MATCH_LEAVE = "match:leave"

# Direct requests callbacks
CB_DR_INBOX = "dr:inbox"
CB_DR_SENT = "dr:sent"
CB_DR_SEND_BY_ID = "dr:send_id"
CB_DR_ACCEPT_PREFIX = "dr:acc:"
CB_DR_REJECT_PREFIX = "dr:rej:"
CB_DR_CANCEL_PREFIX = "dr:can:"
CB_DR_MENU = "dr:menu"

# Active chat inline callbacks
CB_CHAT_END_PREFIX = "chat:end:"
CB_CHAT_BLK_PREFIX = "chat:blk:"

# Connections callbacks
CB_CONN_LIST_PREFIX = "conn:list:"
CB_CONN_OPEN_PREFIX = "conn:open:"
CB_CONN_REQ_PREFIX = "conn:req:"
CB_CONN_REM_PREFIX = "conn:rem:"
CB_CONN_BLK_PREFIX = "conn:blk:"


def get_active_chat_reply_keyboard() -> ReplyKeyboardMarkup:
    """Reply keyboard active while chatting anonymously."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=BTN_END_CHAT),
                KeyboardButton(text=BTN_CHAT_BLOCK),
            ]
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def get_active_chat_inline_keyboard(chat_id: uuid.UUID) -> InlineKeyboardMarkup:
    """Inline keyboard attached to chat start notice with session-scoped callbacks."""
    hex_id = chat_id.hex
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=BTN_END_CHAT,
                    callback_data=f"{CB_CHAT_END_PREFIX}{hex_id}",
                ),
                InlineKeyboardButton(
                    text="🚫 مسدود کردن",
                    callback_data=f"{CB_CHAT_BLK_PREFIX}{hex_id}",
                ),
            ]
        ]
    )


def get_matching_waiting_keyboard() -> InlineKeyboardMarkup:
    """Keyboard shown while user waits in random matching queue."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ لغو جستجو", callback_data=CB_MATCH_LEAVE)],
        ]
    )


def get_matching_entry_keyboard() -> InlineKeyboardMarkup:
    """Keyboard shown to re-enter matching or adjust filters."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎲 شروع جستجوی تصادفی", callback_data=CB_MATCH_JOIN)],
            [InlineKeyboardButton(text="🎛 تنظیم فیلترهای گفتگو", callback_data="disc:prefs")],
        ]
    )


def get_direct_requests_menu_keyboard() -> InlineKeyboardMarkup:
    """Menu keyboard for managing incoming/outgoing direct requests."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📥 درخواست‌های دریافتی", callback_data=CB_DR_INBOX),
                InlineKeyboardButton(text="📤 درخواست‌های ارسالی", callback_data=CB_DR_SENT),
            ],
            [
                InlineKeyboardButton(
                    text="🆔 ارسال درخواست با شناسه عمومی",
                    callback_data=CB_DR_SEND_BY_ID,
                )
            ],
        ]
    )


def get_direct_request_decision_keyboard(request_id: int) -> InlineKeyboardMarkup:
    """Accept/reject inline buttons sent to direct request recipient."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ پذیرش و شروع گفتگو",
                    callback_data=f"{CB_DR_ACCEPT_PREFIX}{request_id}",
                ),
                InlineKeyboardButton(
                    text="❌ رد درخواست",
                    callback_data=f"{CB_DR_REJECT_PREFIX}{request_id}",
                ),
            ]
        ]
    )


def get_outgoing_request_keyboard(request_id: int) -> InlineKeyboardMarkup:
    """Cancel button for an outgoing pending direct request."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❌ لغو درخواست",
                    callback_data=f"{CB_DR_CANCEL_PREFIX}{request_id}",
                )
            ]
        ]
    )


def get_connections_list_keyboard(
    connections: list[ConnectionItemDTO],
    *,
    offset: int = 0,
    page_size: int = 5,
) -> InlineKeyboardMarkup:
    """Paginated inline list of user's connections."""
    slice_items = connections[offset : offset + page_size]
    rows: list[list[InlineKeyboardButton]] = []
    for item in slice_items:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"👤 {item.display_name} ({item.age} سال)",
                    callback_data=f"{CB_CONN_OPEN_PREFIX}{item.public_id}",
                )
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    if offset > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ قبلی",
                callback_data=f"{CB_CONN_LIST_PREFIX}{max(0, offset - page_size)}",
            )
        )
    if offset + page_size < len(connections):
        nav_row.append(
            InlineKeyboardButton(
                text="بعدی ➡️",
                callback_data=f"{CB_CONN_LIST_PREFIX}{offset + page_size}",
            )
        )
    if nav_row:
        rows.append(nav_row)

    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_connection_detail_keyboard(
    public_id: str,
    *,
    allow_request: bool = True,
) -> InlineKeyboardMarkup:
    """Actions for an opened connection: request chat, remove asymmetrically, or block."""
    rows: list[list[InlineKeyboardButton]] = []
    if allow_request:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✉️ ارسال درخواست گفتگو",
                    callback_data=f"{CB_CONN_REQ_PREFIX}{public_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🗑 حذف از ارتباط‌ها",
                callback_data=f"{CB_CONN_REM_PREFIX}{public_id}",
            ),
            InlineKeyboardButton(
                text="🚫 مسدود کردن",
                callback_data=f"{CB_CONN_BLK_PREFIX}{public_id}",
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 بازگشت به لیست ارتباط‌ها",
                callback_data=f"{CB_CONN_LIST_PREFIX}0",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
