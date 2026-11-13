"""Role-aware Persian inline keyboards for the Telegram admin & moderation panel."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.app.modules.admin.rbac import (
    MODERATOR_CONTROL_ROLES,
    ROLE_MODERATOR,
    ROLE_SUPER_ADMIN,
    ROLE_SUPPORT,
    SUPER_ADMIN_ONLY_ROLES,
)
from src.app.modules.admin.schemas import (
    AdminAuditLogPageDTO,
    AdminRoleDTO,
    AdminUserDetailDTO,
    ReportDetailDTO,
    ReportQueuePageDTO,
    SystemConfigItemDTO,
)
from src.app.modules.moderation.schemas import REPORT_CATEGORIES
from src.app.modules.wallet.schemas import TransactionHistoryPageDTO

# Static admin callbacks
CB_ADM_HOME = "adm:home"
CB_ADM_CANCEL = "adm:cancel"
CB_ADM_USER_LOOKUP = "adm:ulookup"
CB_ADM_PAIR_MENU = "adm:pairmenu"
CB_ADM_PAIR_BLK = "adm:pairblk"
CB_ADM_PAIR_UNBLK = "adm:pairunblk"
CB_ADM_WALLET_MENU = "adm:walmenu"
CB_ADM_CFG_LIST = "adm:cfglist"
CB_ADM_ROLES = "adm:roles"
CB_ADM_ROLE_ADD = "adm:roleadd"

# Prefix callbacks (all <= 64 UTF-8 bytes)
CB_ADM_REP_QUEUE_PREFIX = "adm:repq:"
CB_ADM_REP_OPEN_PREFIX = "adm:repopen:"
CB_ADM_REP_RESOLVE_PREFIX = "adm:rres:"
CB_ADM_REP_MOD_PREFIX = "adm:rmod:"
CB_ADM_REP_PAIRBLK_PREFIX = "adm:rblk:"
CB_ADM_USER_VIEW_PREFIX = "adm:uview:"
CB_ADM_MOD_PREFIX = "adm:mod:"
CB_ADM_WALLET_OPEN_PREFIX = "adm:walopen:"
CB_ADM_WALLET_CREDIT_PREFIX = "adm:walcr:"
CB_ADM_WALLET_DEBIT_PREFIX = "adm:waldb:"
CB_ADM_WALLET_LEDGER_PREFIX = "adm:walle:"
CB_ADM_WALLET_CONFIRM_PREFIX = "adm:walconf:"
CB_ADM_CFG_SELECT_PREFIX = "adm:cfg:"
CB_ADM_CFG_CONFIRM_PREFIX = "adm:cfgconf:"
CB_ADM_AUDIT_PREFIX = "adm:audit:"
CB_ADM_ROLE_PICK_PREFIX = "adm:rolepick:"
CB_ADM_ROLE_REVOKE_PREFIX = "adm:rolerev:"
CB_ADM_ROLE_CONFIRM_PREFIX = "adm:roleconf:"

MOD_ACTION_CODES: dict[str, str] = {
    "WARN": "WARNING",
    "CHATR": "CHAT_RESTRICTION",
    "SUSP": "TEMPORARY_SUSPENSION",
    "BAN": "PERMANENT_BAN",
    "REST": "RESTORE",
}


def get_admin_main_menu_keyboard(role: str) -> InlineKeyboardMarkup:
    """Build role-tailored admin panel inline menu."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="📋 صف گزارش‌های در انتظار",
                callback_data=f"{CB_ADM_REP_QUEUE_PREFIX}PENDING:0",
            ),
            InlineKeyboardButton(
                text="🔎 جستجوی کاربر (شناسه عمومی)",
                callback_data=CB_ADM_USER_LOOKUP,
            ),
        ]
    ]

    if role in MODERATOR_CONTROL_ROLES:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🚫 مسدودسازی / رفع مسدودیت بین دو کاربر",
                    callback_data=CB_ADM_PAIR_MENU,
                )
            ]
        )

    if role in SUPER_ADMIN_ONLY_ROLES:
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="🪙 مدیریت کیف پول کاربر",
                        callback_data=CB_ADM_WALLET_MENU,
                    ),
                    InlineKeyboardButton(
                        text="⚙️ تنظیمات سیستم",
                        callback_data=CB_ADM_CFG_LIST,
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="📜 گزارش ممیزی ادمین",
                        callback_data=f"{CB_ADM_AUDIT_PREFIX}0",
                    ),
                    InlineKeyboardButton(
                        text="🛡 مدیریت نقش‌های ادمین",
                        callback_data=CB_ADM_ROLES,
                    ),
                ],
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_admin_cancel_keyboard() -> InlineKeyboardMarkup:
    """Cancel ongoing FSM input and return to admin home."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 بازگشت و انصراف", callback_data=CB_ADM_CANCEL)]
        ]
    )


def get_report_queue_keyboard(page: ReportQueuePageDTO) -> InlineKeyboardMarkup:
    """Paginated moderation queue with status filter tabs."""
    rows: list[list[InlineKeyboardButton]] = []

    for item in page.items:
        cat_label = REPORT_CATEGORIES.get(item.category, item.category)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"#{item.id} | {cat_label} | {item.reported_public_id}",
                    callback_data=f"{CB_ADM_REP_OPEN_PREFIX}{item.id}",
                )
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    if page.has_prev:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ قبلی",
                callback_data=(
                    f"{CB_ADM_REP_QUEUE_PREFIX}{page.status_filter}:"
                    f"{max(0, page.offset - page.limit)}"
                ),
            )
        )
    if page.has_next:
        nav_row.append(
            InlineKeyboardButton(
                text="بعدی ➡️",
                callback_data=(
                    f"{CB_ADM_REP_QUEUE_PREFIX}{page.status_filter}:{page.offset + page.limit}"
                ),
            )
        )
    if nav_row:
        rows.append(nav_row)

    rows.append(
        [
            InlineKeyboardButton(
                text="⏳ در انتظار",
                callback_data=f"{CB_ADM_REP_QUEUE_PREFIX}PENDING:0",
            ),
            InlineKeyboardButton(
                text="✅ اقدام‌شده",
                callback_data=f"{CB_ADM_REP_QUEUE_PREFIX}ACTIONED:0",
            ),
            InlineKeyboardButton(
                text="📄 همه",
                callback_data=f"{CB_ADM_REP_QUEUE_PREFIX}ALL:0",
            ),
        ]
    )
    rows.append([InlineKeyboardButton(text="🏠 منوی ادمین", callback_data=CB_ADM_HOME)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_report_detail_keyboard(detail: ReportDetailDTO, role: str) -> InlineKeyboardMarkup:
    """Action keyboard when inspecting a specific report."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="🔎 پرونده کاربر گزارش‌شده",
                callback_data=f"{CB_ADM_USER_VIEW_PREFIX}{detail.reported_public_id}",
            ),
            InlineKeyboardButton(
                text="⚠️ ثبت اخطار",
                callback_data=(
                    f"{CB_ADM_REP_MOD_PREFIX}WARN:{detail.id}:{detail.reported_public_id}"
                ),
            ),
        ]
    ]

    if role in MODERATOR_CONTROL_ROLES:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔇 محدودیت گفتگو",
                    callback_data=(
                        f"{CB_ADM_REP_MOD_PREFIX}CHATR:{detail.id}:{detail.reported_public_id}"
                    ),
                ),
                InlineKeyboardButton(
                    text="⏳ تعلیق موقت",
                    callback_data=(
                        f"{CB_ADM_REP_MOD_PREFIX}SUSP:{detail.id}:{detail.reported_public_id}"
                    ),
                ),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="🚫 مسدود کردن ارتباط این دو کاربر",
                    callback_data=(
                        f"{CB_ADM_REP_PAIRBLK_PREFIX}{detail.id}:"
                        f"{detail.reporter_public_id}:{detail.reported_public_id}"
                    ),
                )
            ]
        )
        if detail.status == "PENDING":
            rows.append(
                [
                    InlineKeyboardButton(
                        text="👁 بررسی‌شده",
                        callback_data=f"{CB_ADM_REP_RESOLVE_PREFIX}REVIEWED:{detail.id}",
                    ),
                    InlineKeyboardButton(
                        text="✅ اقدام‌شده",
                        callback_data=f"{CB_ADM_REP_RESOLVE_PREFIX}ACTIONED:{detail.id}",
                    ),
                    InlineKeyboardButton(
                        text="🗑 رد گزارش",
                        callback_data=f"{CB_ADM_REP_RESOLVE_PREFIX}DISMISSED:{detail.id}",
                    ),
                ]
            )

    if role in SUPER_ADMIN_ONLY_ROLES:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⛔ مسدودسازی دائم کاربر",
                    callback_data=(
                        f"{CB_ADM_REP_MOD_PREFIX}BAN:{detail.id}:{detail.reported_public_id}"
                    ),
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="📋 بازگشت به صف گزارش‌ها",
                callback_data=f"{CB_ADM_REP_QUEUE_PREFIX}PENDING:0",
            ),
            InlineKeyboardButton(text="🏠 منوی ادمین", callback_data=CB_ADM_HOME),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_user_inspection_keyboard(detail: AdminUserDetailDTO, role: str) -> InlineKeyboardMarkup:
    """Role-filtered moderation & inspection action buttons for a target user."""
    pid = detail.public_id
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="⚠️ ثبت اخطار (WARNING)",
                callback_data=f"{CB_ADM_MOD_PREFIX}WARN:{pid}",
            )
        ]
    ]

    if role in MODERATOR_CONTROL_ROLES:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔇 محدودیت گفتگو",
                    callback_data=f"{CB_ADM_MOD_PREFIX}CHATR:{pid}",
                ),
                InlineKeyboardButton(
                    text="⏳ تعلیق موقت",
                    callback_data=f"{CB_ADM_MOD_PREFIX}SUSP:{pid}",
                ),
            ]
        )

    if role in SUPER_ADMIN_ONLY_ROLES:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⛔ مسدودسازی دائم",
                    callback_data=f"{CB_ADM_MOD_PREFIX}BAN:{pid}",
                ),
                InlineKeyboardButton(
                    text="🪙 کیف پول کاربر",
                    callback_data=f"{CB_ADM_WALLET_OPEN_PREFIX}{pid}",
                ),
            ]
        )

    can_restore = (
        detail.account_status == "SUSPENDED"
        or detail.is_chat_restricted
        or (detail.account_status == "BANNED" and role in SUPER_ADMIN_ONLY_ROLES)
    )
    if role in MODERATOR_CONTROL_ROLES and can_restore:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ رفع محدودیت / بازیابی حساب",
                    callback_data=f"{CB_ADM_MOD_PREFIX}REST:{pid}",
                )
            ]
        )

    rows.append([InlineKeyboardButton(text="🏠 منوی ادمین", callback_data=CB_ADM_HOME)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_pair_block_menu_keyboard() -> InlineKeyboardMarkup:
    """Inline menu for moderator user-pair block / unblock operations."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚫 ایجاد مسدودسازی بین دو کاربر",
                    callback_data=CB_ADM_PAIR_BLK,
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔓 حذف مسدودسازی بین دو کاربر",
                    callback_data=CB_ADM_PAIR_UNBLK,
                )
            ],
            [InlineKeyboardButton(text="🏠 منوی ادمین", callback_data=CB_ADM_HOME)],
        ]
    )


def get_wallet_admin_keyboard(
    public_id: str,
    history_page: TransactionHistoryPageDTO | None = None,
) -> InlineKeyboardMarkup:
    """Wallet management actions for SUPER_ADMIN."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="➕ افزایش سکه خریداری‌شده",
                callback_data=f"{CB_ADM_WALLET_CREDIT_PREFIX}{public_id}",
            ),
            InlineKeyboardButton(
                text="➖ کسر سکه",
                callback_data=f"{CB_ADM_WALLET_DEBIT_PREFIX}{public_id}",
            ),
        ]
    ]
    if history_page is not None:
        nav_row: list[InlineKeyboardButton] = []
        if history_page.has_prev:
            nav_row.append(
                InlineKeyboardButton(
                    text="⬅️ تراکنش‌های جدیدتر",
                    callback_data=(
                        f"{CB_ADM_WALLET_LEDGER_PREFIX}{public_id}:"
                        f"{max(0, history_page.offset - history_page.limit)}"
                    ),
                )
            )
        if history_page.has_next:
            nav_row.append(
                InlineKeyboardButton(
                    text="تراکنش‌های قدیمی‌تر ➡️",
                    callback_data=(
                        f"{CB_ADM_WALLET_LEDGER_PREFIX}{public_id}:"
                        f"{history_page.offset + history_page.limit}"
                    ),
                )
            )
        if nav_row:
            rows.append(nav_row)
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="📜 مشاهده دفترکل تراکنش‌ها",
                    callback_data=f"{CB_ADM_WALLET_LEDGER_PREFIX}{public_id}:0",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                text="🔎 پرونده کاربر",
                callback_data=f"{CB_ADM_USER_VIEW_PREFIX}{public_id}",
            ),
            InlineKeyboardButton(text="🏠 منوی ادمین", callback_data=CB_ADM_HOME),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_confirm_action_keyboard(confirm_callback: str) -> InlineKeyboardMarkup:
    """Two-step confirmation keyboard for sensitive mutations."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ تأیید نهایی و ثبت", callback_data=confirm_callback),
                InlineKeyboardButton(text="❌ انصراف", callback_data=CB_ADM_CANCEL),
            ]
        ]
    )


def get_system_configs_keyboard(items: list[SystemConfigItemDTO]) -> InlineKeyboardMarkup:
    """List allowlisted system configurations for SUPER_ADMIN editing."""
    rows = [
        [
            InlineKeyboardButton(
                text=f"⚙️ {item.label_persian}: {item.effective_int_value}",
                callback_data=f"{CB_ADM_CFG_SELECT_PREFIX}{item.key}",
            )
        ]
        for item in items
    ]
    rows.append([InlineKeyboardButton(text="🏠 منوی ادمین", callback_data=CB_ADM_HOME)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_audit_logs_keyboard(page: AdminAuditLogPageDTO) -> InlineKeyboardMarkup:
    """Pagination keyboard for append-only admin audit logs."""
    rows: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if page.has_prev:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ جدیدتر",
                callback_data=f"{CB_ADM_AUDIT_PREFIX}{max(0, page.offset - page.limit)}",
            )
        )
    if page.has_next:
        nav_row.append(
            InlineKeyboardButton(
                text="قدیمی‌تر ➡️",
                callback_data=f"{CB_ADM_AUDIT_PREFIX}{page.offset + page.limit}",
            )
        )
    if nav_row:
        rows.append(nav_row)
    rows.append([InlineKeyboardButton(text="🏠 منوی ادمین", callback_data=CB_ADM_HOME)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_roles_management_keyboard(roles: list[AdminRoleDTO]) -> InlineKeyboardMarkup:
    """Inline keyboard for listing and managing operator roles."""
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="➕ اعطا یا تغییر نقش مدیر",
                callback_data=CB_ADM_ROLE_ADD,
            )
        ]
    ]
    for item in roles:
        if item.is_active:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"🚫 لغو دسترسی {item.public_id} ({item.role})",
                        callback_data=f"{CB_ADM_ROLE_REVOKE_PREFIX}{item.public_id}",
                    )
                ]
            )
    rows.append([InlineKeyboardButton(text="🏠 منوی ادمین", callback_data=CB_ADM_HOME)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_role_pick_keyboard() -> InlineKeyboardMarkup:
    """Select an admin role to assign."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="پشتیبان (SUPPORT)",
                    callback_data=f"{CB_ADM_ROLE_PICK_PREFIX}{ROLE_SUPPORT}",
                ),
                InlineKeyboardButton(
                    text="ناظر (MODERATOR)",
                    callback_data=f"{CB_ADM_ROLE_PICK_PREFIX}{ROLE_MODERATOR}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="سوپر ادمین (SUPER_ADMIN)",
                    callback_data=f"{CB_ADM_ROLE_PICK_PREFIX}{ROLE_SUPER_ADMIN}",
                )
            ],
            [InlineKeyboardButton(text="❌ انصراف", callback_data=CB_ADM_CANCEL)],
        ]
    )
