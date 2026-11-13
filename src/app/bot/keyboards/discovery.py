"""Inline keyboards for profile discovery browsing, lookup, and preference filtering."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.app.modules.discovery.schemas import DiscoveryPageDTO, PublicProfileDTO, UserPreferencesDTO

# Callback prefixes and static actions (all compact <= 42 bytes)
CB_DISC_BROWSE_PREFIX = "disc:browse:"
CB_DISC_OPEN_PREFIX = "disc:open:"
CB_DISC_REQ_PREFIX = "disc:req:"
CB_DISC_BLK_PREFIX = "disc:blk:"
CB_DISC_PREFS = "disc:prefs"
CB_DISC_LOOKUP = "disc:lookup"

CB_PREF_GENDER_PREFIX = "disc:pref:g:"
CB_PREF_GENDER_CLEAR = "disc:pref:gclear"
CB_PREF_AGE = "disc:pref:age"
CB_PREF_AGE_CLEAR = "disc:pref:ageclear"
CB_PREF_CITY = "disc:pref:city"
CB_PREF_CITY_CLEAR = "disc:pref:cityclear"
CB_PREF_ACT_PREFIX = "disc:pref:act:"
CB_PREF_REL_PREFIX = "disc:pref:rel:"
CB_PREF_CANCEL_INPUT = "disc:pref:cancel"


def get_discovery_profile_keyboard(
    profile: PublicProfileDTO,
    page: DiscoveryPageDTO,
) -> InlineKeyboardMarkup:
    """Build inline keyboard for a discovered public profile with actions and pagination."""
    relaxed_flag = 1 if page.used_relaxed else 0
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="✉️ ارسال درخواست گفتگو",
                callback_data=f"{CB_DISC_REQ_PREFIX}{profile.public_id}",
            ),
            InlineKeyboardButton(
                text="🚫 مسدود کردن",
                callback_data=f"{CB_DISC_BLK_PREFIX}{profile.public_id}",
            ),
        ]
    ]

    nav_row: list[InlineKeyboardButton] = []
    if page.has_prev:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️ قبلی",
                callback_data=f"{CB_DISC_BROWSE_PREFIX}{page.offset - 1}:{relaxed_flag}",
            )
        )
    if page.has_next:
        nav_row.append(
            InlineKeyboardButton(
                text="پروفایل بعدی ➡️",
                callback_data=f"{CB_DISC_BROWSE_PREFIX}{page.offset + 1}:{relaxed_flag}",
            )
        )
    if nav_row:
        rows.append(nav_row)

    rows.append(
        [
            InlineKeyboardButton(text="🎛 فیلترهای جستجو", callback_data=CB_DISC_PREFS),
            InlineKeyboardButton(text="🆔 جستجو با شناسه", callback_data=CB_DISC_LOOKUP),
        ]
    )

    if page.relaxed_available and not page.used_relaxed:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🔓 نمایش پیشنهادهای قابل انعطاف ({page.relaxed_count})",
                    callback_data=f"{CB_DISC_BROWSE_PREFIX}0:1",
                )
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_empty_discovery_keyboard(page: DiscoveryPageDTO) -> InlineKeyboardMarkup:
    """Build inline keyboard when no exact profiles match current filters."""
    rows: list[list[InlineKeyboardButton]] = []
    if page.relaxed_available and not page.used_relaxed:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🔓 نمایش با فیلترهای قابل انعطاف ({page.relaxed_count})",
                    callback_data=f"{CB_DISC_BROWSE_PREFIX}0:1",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="🎛 تنظیم فیلترهای جستجو", callback_data=CB_DISC_PREFS),
            InlineKeyboardButton(text="🆔 جستجو با شناسه", callback_data=CB_DISC_LOOKUP),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_public_profile_detail_keyboard(
    profile: PublicProfileDTO,
    *,
    allow_request: bool = True,
) -> InlineKeyboardMarkup:
    """Build inline keyboard when opening a profile directly by public_id."""
    rows: list[list[InlineKeyboardButton]] = []
    if allow_request and not profile.in_active_chat:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✉️ ارسال درخواست گفتگو",
                    callback_data=f"{CB_DISC_REQ_PREFIX}{profile.public_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="🚫 مسدود کردن کاربر",
                callback_data=f"{CB_DISC_BLK_PREFIX}{profile.public_id}",
            ),
            InlineKeyboardButton(
                text="🔎 مرور پروفایل‌ها",
                callback_data=f"{CB_DISC_BROWSE_PREFIX}0:0",
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_preferences_keyboard(prefs: UserPreferencesDTO) -> InlineKeyboardMarkup:
    """Build inline keyboard to configure required and relaxable discovery filters."""

    def _g_mark(code: str, label: str) -> str:
        prefix = "✅ " if code in prefs.preferred_genders else ""
        return f"{prefix}{label}"

    def _act_mark(hours: int | None, label: str) -> str:
        prefix = "✅ " if prefs.recent_activity_hours == hours else ""
        return f"{prefix}{label}"

    def _rel_mark(field: str, label: str) -> str:
        prefix = "🔓 " if field in prefs.relaxable_fields else "🔒 "
        return f"{prefix}{label}"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_g_mark("MALE", "مرد"),
                    callback_data=f"{CB_PREF_GENDER_PREFIX}MALE",
                ),
                InlineKeyboardButton(
                    text=_g_mark("FEMALE", "زن"),
                    callback_data=f"{CB_PREF_GENDER_PREFIX}FEMALE",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_g_mark("NON_BINARY", "غیردوگانه"),
                    callback_data=f"{CB_PREF_GENDER_PREFIX}NON_BINARY",
                ),
                InlineKeyboardButton(
                    text=_g_mark("OTHER", "سایر"),
                    callback_data=f"{CB_PREF_GENDER_PREFIX}OTHER",
                ),
                InlineKeyboardButton(
                    text="همه جنسیت‌ها",
                    callback_data=CB_PREF_GENDER_CLEAR,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"🎂 سن: {prefs.min_age} تا {prefs.max_age}",
                    callback_data=CB_PREF_AGE,
                ),
                InlineKeyboardButton(
                    text="بازنشانی سن",
                    callback_data=CB_PREF_AGE_CLEAR,
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🏙 تنظیم شهرهای دلخواه",
                    callback_data=CB_PREF_CITY,
                ),
                InlineKeyboardButton(
                    text="همه شهرها",
                    callback_data=CB_PREF_CITY_CLEAR,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_act_mark(None, "همه زمان‌ها"),
                    callback_data=f"{CB_PREF_ACT_PREFIX}0",
                ),
                InlineKeyboardButton(
                    text=_act_mark(24, "۲۴ ساعت اخیر"),
                    callback_data=f"{CB_PREF_ACT_PREFIX}24",
                ),
                InlineKeyboardButton(
                    text=_act_mark(168, "۷ روز اخیر"),
                    callback_data=f"{CB_PREF_ACT_PREFIX}168",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_rel_mark("gender", "انعطاف جنسیت"),
                    callback_data=f"{CB_PREF_REL_PREFIX}gender",
                ),
                InlineKeyboardButton(
                    text=_rel_mark("age", "انعطاف سن"),
                    callback_data=f"{CB_PREF_REL_PREFIX}age",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_rel_mark("city", "انعطاف شهر"),
                    callback_data=f"{CB_PREF_REL_PREFIX}city",
                ),
                InlineKeyboardButton(
                    text=_rel_mark("recent_activity", "انعطاف فعالیت"),
                    callback_data=f"{CB_PREF_REL_PREFIX}recent_activity",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔎 مشاهده پروفایل‌ها با این فیلترها",
                    callback_data=f"{CB_DISC_BROWSE_PREFIX}0:0",
                )
            ],
        ]
    )


def get_cancel_filter_input_keyboard() -> InlineKeyboardMarkup:
    """Cancel keyboard while inputting text for age range, cities, or public_id."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 بازگشت", callback_data=CB_PREF_CANCEL_INPUT)]
        ]
    )
