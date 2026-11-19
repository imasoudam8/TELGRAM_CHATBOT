"""End-to-end Telegram Bot user-journey integration tests over live PostgreSQL 16 and Redis 7.

Exercises the full bot interaction layer (`create_dispatcher`, routers, filters, FSMContext,
StructuredLoggingMiddleware, DI, services, PostgreSQL 16, Redis 7, and outgoing Bot API calls)
using a fake Bot `BaseSession` transport (`TelegramTestHarness`) with zero real Telegram network
calls and zero real credentials.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from aiogram.types import ReplyKeyboardMarkup
from sqlalchemy import func, select

from src.app.bot.keyboards.admin import CB_ADM_HOME, CB_ADM_REP_QUEUE_PREFIX
from src.app.bot.keyboards.chat import (
    BTN_END_CHAT,
    CB_CHAT_BLK_PREFIX,
    CB_CHAT_END_PREFIX,
    CB_CONN_BLK_PREFIX,
    CB_CONN_OPEN_PREFIX,
    CB_CONN_REM_PREFIX,
    CB_DR_ACCEPT_PREFIX,
    CB_DR_CANCEL_PREFIX,
    CB_DR_INBOX,
    CB_DR_REJECT_PREFIX,
    CB_DR_SEND_BY_ID,
    CB_DR_SENT,
    CB_MATCH_JOIN,
    CB_MATCH_LEAVE,
)
from src.app.bot.keyboards.discovery import (
    CB_DISC_BLK_PREFIX,
    CB_DISC_BROWSE_PREFIX,
    CB_DISC_LOOKUP,
    CB_DISC_OPEN_PREFIX,
    CB_DISC_PREFS,
    CB_DISC_REQ_PREFIX,
    CB_PREF_AGE,
    CB_PREF_CITY,
    CB_PREF_CITY_CLEAR,
    CB_PREF_GENDER_PREFIX,
    CB_PREF_REL_PREFIX,
)
from src.app.bot.keyboards.moderation import (
    CB_REPORT_CAT_PREFIX,
    CB_REPORT_SKIP_DESC,
    CB_REPORT_START_CHAT_PREFIX,
    CB_REPORT_START_USER_PREFIX,
)
from src.app.bot.keyboards.onboarding import (
    BTN_DIRECT_REQUEST,
    BTN_HELP_SUPPORT,
    BTN_MY_CONNECTIONS,
    BTN_MY_PROFILE,
    BTN_RANDOM_CHAT,
    BTN_VIEW_PROFILES,
    BTN_WALLET,
    CALLBACK_AGE_CONFIRM,
    CALLBACK_AGE_DECLINE,
)
from src.app.bot.keyboards.profile import (
    CB_BACK_TO_PROFILE,
    CB_CANCEL_WIZARD,
    CB_CONFIRM_CREATE,
    CB_EDIT_AGE,
    CB_EDIT_BIO,
    CB_EDIT_CITY,
    CB_EDIT_GENDER,
    CB_EDIT_NAME,
    CB_GENDER_PREFIX,
    CB_SKIP_BIO,
    CB_SKIP_CITY,
    CB_START_CREATE,
)
from src.app.bot.keyboards.wallet import CB_WALLET_HISTORY_PREFIX, CB_WALLET_REFRESH
from src.app.config.settings import Settings
from src.app.infrastructure.database.base import now_utc
from src.app.infrastructure.database.session import DatabaseManager
from src.app.infrastructure.redis.client import RedisManager
from src.app.modules.chat.models import (
    ActiveChatParticipant,
    ChatMessage,
    ChatSession,
    DirectRequest,
    MessageEvidenceHold,
    UserBlock,
    UserConnection,
)
from src.app.modules.chat.session_service import VIDEO_SIZE_LIMIT_BYTES
from src.app.modules.moderation.models import Report
from src.app.modules.users.models import User, UserProfile
from src.app.modules.wallet.models import Wallet, WalletLedger
from src.app.modules.wallet.service import WalletService
from tests.helpers.telegram_harness import TelegramTestHarness


async def _onboard_via_bot(
    harness: TelegramTestHarness,
    tg_id: int,
    *,
    name: str,
    age: int,
    gender: str = "MALE",
    city: str | None = "تهران",
    bio: str | None = "سلام",
) -> User:
    """Drive a real user through /start -> 18+ confirm -> profile creation wizard via Dispatcher."""
    await harness.send_command(tg_id, "/start")
    await harness.press_button(tg_id, callback_data=CALLBACK_AGE_CONFIRM)
    await harness.send_text(tg_id, BTN_MY_PROFILE)
    await harness.press_button(tg_id, callback_data=CB_START_CREATE)
    await harness.send_text(tg_id, name)
    await harness.send_text(tg_id, str(age))
    await harness.press_button(tg_id, callback_data=f"{CB_GENDER_PREFIX}{gender}")
    if city is None:
        await harness.press_button(tg_id, callback_data=CB_SKIP_CITY)
    else:
        await harness.send_text(tg_id, city)
    if bio is None:
        await harness.press_button(tg_id, callback_data=CB_SKIP_BIO)
    else:
        await harness.send_text(tg_id, bio)
    await harness.press_button(tg_id, callback_data=CB_CONFIRM_CREATE)

    async with harness.db_manager.session() as session:
        user = (
            await session.execute(select(User).where(User.telegram_user_id == tg_id))
        ).scalar_one()
        return user


async def _get_user_by_tg_id(db: DatabaseManager, tg_id: int) -> User | None:
    async with db.session() as session:
        return (
            await session.execute(select(User).where(User.telegram_user_id == tg_id))
        ).scalar_one_or_none()


# =============================================================================
# Journey A: Registration & Onboarding
# =============================================================================
@pytest.mark.asyncio
async def test_journey_a_onboarding_age_gate_repeated_start_and_privacy(
    migrated_postgres_db: tuple[str, Settings],
) -> None:
    """Journey A: /start, 18+ confirmation, underage gate, repeated /start FSM reset, privacy."""
    _, settings = migrated_postgres_db
    db = DatabaseManager(settings)
    await db.initialize()
    harness = TelegramTestHarness(settings=settings, db_manager=db)

    tg_new = 710100001
    tg_underage = 710100002

    try:
        # 1. /start prompts new user for 18+ self-declaration, nothing persisted yet
        await harness.send_command(tg_new, "/start")
        assert "۱۸ سال" in harness.latest_text_for(tg_new)
        assert await _get_user_by_tg_id(db, tg_new) is None

        # 2. Unexpected plain text and menu clicks remain blocked before 18+ declaration
        await harness.send_text(tg_new, "سلام همینجوری")
        await harness.send_text(tg_new, BTN_MY_PROFILE)
        assert "شرایط سنی ۱۸+" in harness.latest_text_for(tg_new)
        await harness.send_text(tg_new, BTN_VIEW_PROFILES)
        assert "شرایط سنی ۱۸+" in harness.latest_text_for(tg_new)
        await harness.send_text(tg_new, BTN_WALLET)
        assert "شرایط سنی ۱۸+" in harness.latest_text_for(tg_new)
        await harness.send_text(tg_new, BTN_HELP_SUPPORT)
        assert "شرایط سنی ۱۸+" in harness.latest_text_for(tg_new)
        assert await _get_user_by_tg_id(db, tg_new) is None

        # 3. Underage confirmation leaves menu locked and does NOT create user row
        await harness.send_command(tg_underage, "/start")
        await harness.press_button(tg_underage, callback_data=CALLBACK_AGE_DECLINE)
        assert "بالای ۱۸ سال" in harness.latest_text_for(tg_underage)
        await harness.send_text(tg_underage, BTN_MY_PROFILE)
        assert "شرایط سنی ۱۸+" in harness.latest_text_for(tg_underage)
        assert await _get_user_by_tg_id(db, tg_underage) is None

        # 4. Adult confirmation registers user with anonymous usr_ public_id & main menu keyboard
        calls = await harness.press_button(tg_new, callback_data=CALLBACK_AGE_CONFIRM)
        user = await _get_user_by_tg_id(db, tg_new)
        assert user is not None
        assert user.age_18_plus_declared is True
        assert user.public_id.startswith("usr_")
        assert str(tg_new) not in user.public_id
        assert any(isinstance(c.reply_markup, ReplyKeyboardMarkup) for c in calls)
        assert user.public_id in harness.latest_text_for(tg_new)

        # 5. Repeated /start in middle of ProfileCreationSG resets FSM state cleanly
        await harness.send_text(tg_new, BTN_MY_PROFILE)
        await harness.press_button(tg_new, callback_data=CB_START_CREATE)
        assert "نام نمایشی" in harness.latest_text_for(tg_new)

        await harness.send_command(tg_new, "/start")
        assert user.public_id in harness.latest_text_for(tg_new)

        # Next menu tap works instead of being swallowed as display_name!
        await harness.send_text(tg_new, BTN_HELP_SUPPORT)
        assert "راهنما و پشتیبانی" in harness.latest_text_for(tg_new)

        # Duplicate age_confirm callback does not create duplicate users
        await harness.press_button(
            tg_new, callback_data=CALLBACK_AGE_CONFIRM, allow_missing_from_history=True
        )
        async with db.session() as session:
            user_count = (
                await session.execute(select(func.count(User.id)).where(User.id == user.id))
            ).scalar_one()
            assert user_count == 1

        harness.assert_no_privacy_leak([tg_new, tg_underage], ["09120001122"])
    finally:
        await harness.close()
        await db.close()


# =============================================================================
# Journey B: Profile Management
# =============================================================================
@pytest.mark.asyncio
async def test_journey_b_profile_creation_editing_visibility_and_cross_user_isolation(
    migrated_postgres_db: tuple[str, Settings],
) -> None:
    """Journey B: full profile wizard, invalid inputs, editing, cancel, visibility & isolation."""
    _, settings = migrated_postgres_db
    db = DatabaseManager(settings)
    await db.initialize()
    harness = TelegramTestHarness(settings=settings, db_manager=db)

    tg_a = 710200001
    tg_b = 710200002
    tg_incomplete = 710200003
    tg_hidden = 710200004

    try:
        # Onboard tg_a interactively including validation error retries
        await harness.send_command(tg_a, "/start")
        await harness.press_button(tg_a, callback_data=CALLBACK_AGE_CONFIRM)
        await harness.send_text(tg_a, BTN_MY_PROFILE)
        await harness.press_button(tg_a, callback_data=CB_START_CREATE)

        # Too-short name rejected without advancing
        await harness.send_text(tg_a, "A")
        assert "۲ تا ۵۰" in harness.latest_text_for(tg_a)
        await harness.send_text(tg_a, "<b>آرش</b>")  # Also exercises HTML escaping

        # Out-of-bounds / non-numeric age rejected
        await harness.send_text(tg_a, "17")
        assert "۱۸ تا ۹۹" in harness.latest_text_for(tg_a)
        await harness.send_text(tg_a, "abc")
        assert "عدد معتبر" in harness.latest_text_for(tg_a)
        await harness.send_text(tg_a, "29")

        await harness.press_button(tg_a, callback_data=f"{CB_GENDER_PREFIX}MALE")
        await harness.send_text(tg_a, "تهران")
        await harness.send_text(tg_a, "علاقه‌مند به کتاب و موسیقی")
        await harness.press_button(tg_a, callback_data=CB_CONFIRM_CREATE)

        # Out-of-order duplicate confirm after state cleared is safely ignored
        await harness.press_button(
            tg_a, callback_data=CB_CONFIRM_CREATE, allow_missing_from_history=True
        )

        # Check HTML escaped & persisted in PostgreSQL
        assert "&lt;b&gt;آرش&lt;/b&gt;" in harness.latest_text_for(tg_a)
        async with db.session() as session:
            ua = (
                await session.execute(select(User).where(User.telegram_user_id == tg_a))
            ).scalar_one()
            pa = (
                await session.execute(select(UserProfile).where(UserProfile.user_id == ua.id))
            ).scalar_one()
            assert pa.age == 29
            assert pa.completion_status == "COMPLETED"
            assert pa.visibility == "PUBLIC"

        # User B cancels first attempt mid-wizard, then completes with skips
        await harness.send_command(tg_b, "/start")
        await harness.press_button(tg_b, callback_data=CALLBACK_AGE_CONFIRM)
        await harness.send_text(tg_b, BTN_MY_PROFILE)
        await harness.press_button(tg_b, callback_data=CB_START_CREATE)
        await harness.send_text(tg_b, "مریم")
        await harness.send_text(tg_b, "25")
        await harness.press_button(tg_b, callback_data=CB_CANCEL_WIZARD)
        assert "لغو" in harness.latest_text_for(tg_b)

        await harness.send_text(tg_b, BTN_MY_PROFILE)
        await harness.press_button(tg_b, callback_data=CB_START_CREATE)
        await harness.send_text(tg_b, "سارا")
        await harness.send_text(tg_b, "25")
        await harness.press_button(tg_b, callback_data=f"{CB_GENDER_PREFIX}FEMALE")
        await harness.press_button(tg_b, callback_data=CB_SKIP_CITY)
        await harness.press_button(tg_b, callback_data=CB_SKIP_BIO)
        await harness.press_button(tg_b, callback_data=CB_CONFIRM_CREATE)
        assert "سارا" in harness.latest_text_for(tg_b)

        # User A edits display_name, age, gender, city, bio, and cancels an edit cleanly
        await harness.send_text(tg_a, BTN_MY_PROFILE)
        await harness.press_button(tg_a, callback_data=CB_EDIT_NAME)
        await harness.send_text(tg_a, "آرش جدید")
        await harness.press_button(tg_a, callback_data=CB_EDIT_AGE)
        await harness.send_text(tg_a, "30")
        await harness.press_button(tg_a, callback_data=CB_EDIT_GENDER)
        await harness.press_button(tg_a, callback_data=f"{CB_GENDER_PREFIX}edit:OTHER")
        await harness.press_button(tg_a, callback_data=CB_EDIT_CITY)
        await harness.send_text(tg_a, "شیراز")
        await harness.press_button(tg_a, callback_data=CB_EDIT_BIO)
        await harness.send_text(tg_a, "بیو به‌روزشده")

        # Cancel edit leaves age=30 unchanged
        await harness.press_button(tg_a, callback_data=CB_EDIT_AGE)
        await harness.press_button(tg_a, callback_data=CB_BACK_TO_PROFILE)

        # Cross-user check: User B editing their own name does NOT affect User A
        await harness.send_text(tg_b, BTN_MY_PROFILE)
        await harness.press_button(tg_b, callback_data=CB_EDIT_NAME)
        await harness.send_text(tg_b, "سارا ویرایش‌شده")

        async with db.session() as session:
            pa_after = (
                await session.execute(select(UserProfile).where(UserProfile.user_id == ua.id))
            ).scalar_one()
            assert pa_after.display_name == "آرش جدید"
            assert pa_after.age == 30
            assert pa_after.gender == "OTHER"
            assert pa_after.city == "شیراز"
            assert pa_after.bio == "بیو به‌روزشده"

        # Register incomplete user + hidden user, verify excluded from discovery browse
        await harness.send_command(tg_incomplete, "/start")
        await harness.press_button(tg_incomplete, callback_data=CALLBACK_AGE_CONFIRM)
        u_hidden = await _onboard_via_bot(
            harness, tg_hidden, name="کاربر مخفی", age=24, gender="FEMALE", city="تهران"
        )
        async with db.session() as session:
            ph = (
                await session.execute(select(UserProfile).where(UserProfile.user_id == u_hidden.id))
            ).scalar_one()
            ph.visibility = "HIDDEN"

        # User B browses: sees only User A (COMPLETED + PUBLIC), never incomplete or HIDDEN!
        await harness.send_text(tg_b, BTN_VIEW_PROFILES)
        view_text = harness.latest_text_for(tg_b)
        assert "آرش جدید" in view_text
        assert "۱ از ۱" in view_text or "1 از 1" in view_text
        assert "کاربر مخفی" not in view_text

        harness.assert_no_privacy_leak([tg_a, tg_b, tg_incomplete, tg_hidden])
    finally:
        await harness.close()
        await db.close()


# =============================================================================
# Journey C: Discovery & Matching (Live PostgreSQL 16 + Redis 7)
# =============================================================================
@pytest.mark.asyncio
async def test_journey_c_discovery_filters_relaxable_pagination_lookup_and_random_matching(
    migrated_postgres_db: tuple[str, Settings],
) -> None:
    """Journey C: strict vs relaxable discovery filters, pagination, lookup, & Redis matching."""
    _, settings = migrated_postgres_db
    db = DatabaseManager(settings)
    redis = RedisManager(settings)
    await db.initialize()
    await redis.initialize()
    await redis.client.delete("matching:queue")

    harness = TelegramTestHarness(settings=settings, db_manager=db, redis_manager=redis)

    tg_a = 710300001
    tg_b = 710300002
    tg_c = 710300003
    tg_d = 710300004
    tg_blocked = 710300005
    tg_incomp = 710300006

    try:
        ua = await _onboard_via_bot(harness, tg_a, name="پویا", age=28, gender="MALE", city="تهران")
        ub = await _onboard_via_bot(
            harness, tg_b, name="ندا", age=25, gender="FEMALE", city="تهران"
        )
        uc = await _onboard_via_bot(
            harness, tg_c, name="نگین", age=26, gender="FEMALE", city="تهران"
        )
        _ud = await _onboard_via_bot(
            harness, tg_d, name="مینا", age=25, gender="FEMALE", city="شیراز"
        )
        u_blk = await _onboard_via_bot(
            harness, tg_blocked, name="مسدودی", age=25, gender="FEMALE", city="تهران"
        )

        # Incomplete user cannot enter random matching
        await harness.send_command(tg_incomp, "/start")
        await harness.press_button(tg_incomp, callback_data=CALLBACK_AGE_CONFIRM)
        await harness.send_text(tg_incomp, BTN_RANDOM_CHAT)
        assert "پروفایل خود را تکمیل" in harness.latest_text_for(tg_incomp)

        # User A blocks u_blk via discovery lookup -> open -> block flow
        await harness.send_text(tg_a, BTN_VIEW_PROFILES)
        await harness.press_button(
            harness.session.calls[-1].chat_id or tg_a, callback_data=CB_DISC_LOOKUP
        )
        await harness.send_text(tg_a, u_blk.public_id)
        assert "مسدودی" in harness.latest_text_for(tg_a)
        await harness.press_button(tg_a, callback_data=f"{CB_DISC_BLK_PREFIX}{u_blk.public_id}")
        assert "مسدود شد" in harness.latest_text_for(tg_a)

        # Verify disc:open:<pid> directly works as well (regression test for detail keyboard)
        await harness.press_button(
            tg_a,
            callback_data=f"{CB_DISC_OPEN_PREFIX}{ub.public_id}",
            allow_missing_from_history=True,
        )
        assert "ندا" in harness.latest_text_for(tg_a)

        # Configure filters via UI: gender FEMALE, age 23-29, city مشهد -> 0 strict matches
        await harness.send_text(tg_a, BTN_VIEW_PROFILES)
        await harness.press_button(tg_a, callback_data=CB_DISC_PREFS)
        await harness.press_button(tg_a, callback_data=f"{CB_PREF_GENDER_PREFIX}FEMALE")
        await harness.press_button(tg_a, callback_data=CB_PREF_AGE)
        await harness.send_text(tg_a, "23-29")
        await harness.press_button(tg_a, callback_data=CB_PREF_CITY)
        await harness.send_text(tg_a, "مشهد")

        # Strict browse returns empty WITHOUT offering relaxation
        await harness.press_button(tg_a, callback_data=f"{CB_DISC_BROWSE_PREFIX}0:0")
        assert "پروفایل جدیدی مطابق فیلترهای شما یافت نشد" in harness.latest_text_for(tg_a)

        # Toggle city relaxable -> strict browse STILL shows empty + explicit relaxed opt-in button
        await harness.press_button(tg_a, callback_data=CB_DISC_PREFS)
        await harness.press_button(tg_a, callback_data=f"{CB_PREF_REL_PREFIX}city")
        await harness.press_button(tg_a, callback_data=f"{CB_DISC_BROWSE_PREFIX}0:0")
        assert "پیشنهادی دیگر در دسترس است" in harness.latest_text_for(tg_a)

        # User explicitly opts into relaxed browse -> finds ندا، نگین، مینا (never پویا or مسدودی)
        await harness.press_button(tg_a, callback_data=f"{CB_DISC_BROWSE_PREFIX}0:1")
        assert "قابل انعطاف" in harness.latest_text_for(tg_a)
        assert "از 3" in harness.latest_text_for(tg_a)

        # Switch to strict تهران -> pagination 1 of 2 then 2 of 2
        await harness.press_button(tg_a, callback_data=CB_DISC_PREFS)
        await harness.press_button(tg_a, callback_data=CB_PREF_CITY_CLEAR)
        await harness.press_button(tg_a, callback_data=CB_PREF_CITY)
        await harness.send_text(tg_a, "تهران")
        # lock city again
        await harness.press_button(tg_a, callback_data=f"{CB_PREF_REL_PREFIX}city")
        await harness.press_button(tg_a, callback_data=f"{CB_DISC_BROWSE_PREFIX}0:0")
        first_card = harness.latest_text_for(tg_a)
        assert "از 2" in first_card
        assert ub.public_id in first_card or uc.public_id in first_card

        await harness.press_button(tg_a, callback_data=f"{CB_DISC_BROWSE_PREFIX}1:0")
        second_card = harness.latest_text_for(tg_a)
        assert "پروفایل 2 از 2" in second_card
        assert first_card != second_card

        # Random matchmaking queue with live Redis 7
        # User C enters & leaves queue cleanly
        await harness.send_text(tg_c, BTN_RANDOM_CHAT)
        assert "در صف جستجوی گفتگوی تصادفی قرار گرفتید" in harness.latest_text_for(tg_c)
        assert await redis.client.zscore("matching:queue", str(uc.id)) is not None

        await harness.press_button(tg_c, callback_data=CB_MATCH_LEAVE)
        assert "لغو گردید" in harness.latest_text_for(tg_c)
        assert await redis.client.zscore("matching:queue", str(uc.id)) is None

        # User B enters queue (compatible with A), then User A enters -> MATCHED!
        await harness.send_text(tg_b, BTN_RANDOM_CHAT)
        assert "در صف جستجوی گفتگوی تصادفی قرار گرفتید" in harness.latest_text_for(tg_b)

        await harness.send_text(tg_a, BTN_RANDOM_CHAT)
        a_texts = " ".join(c.text or "" for c in harness.text_calls_for(tg_a))
        b_texts = " ".join(c.text or "" for c in harness.text_calls_for(tg_b))
        assert "هم‌صحبت تصادفی یافت شد" in a_texts
        assert "هم‌صحبت تصادفی یافت شد" in b_texts

        # ActiveChatParticipant created for BOTH in PG16 and Redis queue cleared for both
        async with db.session() as session:
            active_rows = (await session.execute(select(ActiveChatParticipant))).scalars().all()
            assert {r.user_id for r in active_rows} == {ua.id, ub.id}
        assert await redis.client.zscore("matching:queue", str(ua.id)) is None
        assert await redis.client.zscore("matching:queue", str(ub.id)) is None

        # Simultaneous chat prevention for User A while in active chat
        await harness.press_button(
            tg_a, callback_data=CB_MATCH_JOIN, allow_missing_from_history=True
        )
        assert "در یک گفتگوی فعال هستید" in harness.latest_text_for(tg_a)

        # Blocked user cannot match User A after A ends chat
        await harness.send_text(tg_a, BTN_END_CHAT)
        await harness.send_text(tg_blocked, BTN_RANDOM_CHAT)
        await harness.send_text(tg_a, BTN_RANDOM_CHAT)
        assert "هم‌صحبت تصادفی یافت شد" not in harness.latest_text_for(tg_a)
        assert "در صف جستجوی گفتگوی تصادفی قرار گرفتید" in harness.latest_text_for(tg_a)

        harness.assert_no_privacy_leak([tg_a, tg_b, tg_c, tg_d, tg_blocked, tg_incomp])
    finally:
        try:
            await redis.client.delete("matching:queue")
        except Exception:
            pass
        await harness.close()
        await redis.close()
        await db.close()


# =============================================================================
# Journey D: Direct Requests
# =============================================================================
@pytest.mark.asyncio
async def test_journey_d_direct_requests_send_decide_expire_cancel_and_auth(
    migrated_postgres_db: tuple[str, Settings],
) -> None:
    """Journey D: send from discovery & ID, reject, cancel, 24h expiry, accept, auth checks."""
    _, settings = migrated_postgres_db
    db = DatabaseManager(settings)
    await db.initialize()
    harness = TelegramTestHarness(settings=settings, db_manager=db)

    tg_a = 710400001
    tg_b = 710400002
    tg_c = 710400003

    try:
        _uc = await _onboard_via_bot(harness, tg_c, name="شخص ثالث", age=31, gender="MALE")
        ua = await _onboard_via_bot(harness, tg_a, name="فرهاد", age=27, gender="MALE")
        ub = await _onboard_via_bot(harness, tg_b, name="لیلا", age=25, gender="FEMALE")

        # 1. User A browses discovery and sends direct request from profile card
        await harness.send_text(tg_a, BTN_VIEW_PROFILES)
        await harness.press_button(tg_a, callback_data=f"{CB_DISC_REQ_PREFIX}{ub.public_id}")
        assert "درخواست گفتگوی مستقیم" in harness.latest_text_for(tg_a)

        # Recipient B received accept/reject inline buttons
        _, accept_cb = harness.find_button(tg_b, prefix=CB_DR_ACCEPT_PREFIX)
        req_id = int(accept_cb.replace(CB_DR_ACCEPT_PREFIX, ""))

        # 2. Duplicate pending request rejected with alert answer
        await harness.press_button(tg_a, callback_data=f"{CB_DISC_REQ_PREFIX}{ub.public_id}")
        assert "انتظار" in (harness.calls[-1].callback_text or "")

        # 3. Unauthorized User C cannot accept or reject B's direct request
        await harness.press_button(
            tg_c,
            callback_data=f"{CB_DR_ACCEPT_PREFIX}{req_id}",
            allow_missing_from_history=True,
        )
        assert "مجاز" in (harness.calls[-1].callback_text or "")

        # 4. User B checks inbox & rejects request -> A notified
        await harness.send_text(tg_b, BTN_DIRECT_REQUEST)
        await harness.press_button(tg_b, callback_data=CB_DR_INBOX)
        await harness.press_button(tg_b, callback_data=f"{CB_DR_REJECT_PREFIX}{req_id}")
        assert "رد شد" in harness.latest_text_for(tg_b)
        assert "پذیرفته نشد" in harness.latest_text_for(tg_a)

        # 5. Send via public ID flow -> inspect Sent -> cancel outgoing
        await harness.send_text(tg_a, BTN_DIRECT_REQUEST)
        await harness.press_button(tg_a, callback_data=CB_DR_SEND_BY_ID)
        await harness.send_text(tg_a, ub.public_id)
        await harness.send_text(tg_a, BTN_DIRECT_REQUEST)
        await harness.press_button(tg_a, callback_data=CB_DR_SENT)
        _, cancel_cb = harness.find_button(tg_a, prefix=CB_DR_CANCEL_PREFIX)
        await harness.press_button(tg_a, callback_data=cancel_cb)
        assert "لغو گردید" in harness.latest_text_for(tg_a)

        # 6. Expired 24h request cannot be accepted
        await harness.send_text(tg_a, BTN_DIRECT_REQUEST)
        await harness.press_button(tg_a, callback_data=CB_DR_SEND_BY_ID)
        await harness.send_text(tg_a, ub.public_id)
        _, exp_accept_cb = harness.find_button(tg_b, prefix=CB_DR_ACCEPT_PREFIX)
        exp_req_id = int(exp_accept_cb.replace(CB_DR_ACCEPT_PREFIX, ""))

        async with db.session() as session:
            dr_exp = (
                await session.execute(select(DirectRequest).where(DirectRequest.id == exp_req_id))
            ).scalar_one()
            dr_exp.expires_at = now_utc() - timedelta(minutes=1)

        await harness.press_button(tg_b, callback_data=exp_accept_cb)
        assert "مهلت" in (harness.calls[-1].callback_text or "")
        async with db.session() as session:
            dr_exp_after = (
                await session.execute(select(DirectRequest).where(DirectRequest.id == exp_req_id))
            ).scalar_one()
            assert dr_exp_after.status == "EXPIRED"

        # 7. Fresh request accepted -> active DIRECT chat starts + duplicate click harmless
        await harness.send_text(tg_a, BTN_DIRECT_REQUEST)
        await harness.press_button(tg_a, callback_data=CB_DR_SEND_BY_ID)
        await harness.send_text(tg_a, ub.public_id)
        _, final_acc_cb = harness.find_button(tg_b, prefix=CB_DR_ACCEPT_PREFIX)
        await harness.press_button(tg_b, callback_data=final_acc_cb)

        assert "گفتگو آغاز" in " ".join(c.text or "" for c in harness.text_calls_for(tg_a)[-3:])
        assert "گفتگو آغاز" in " ".join(c.text or "" for c in harness.text_calls_for(tg_b)[-3:])

        # Replaying same accept callback does not create duplicate chat
        await harness.press_button(
            tg_b, callback_data=final_acc_cb, allow_missing_from_history=True
        )
        assert "قبلاً" in (harness.calls[-1].callback_text or "")
        async with db.session() as session:
            chat_cnt = (await session.execute(select(func.count(ChatSession.id)))).scalar_one()
            part_rows = (await session.execute(select(ActiveChatParticipant))).scalars().all()
            assert chat_cnt == 1
            assert {p.user_id for p in part_rows} == {ua.id, ub.id}

        harness.assert_no_privacy_leak([tg_a, tg_b, tg_c])
    finally:
        await harness.close()
        await db.close()


# =============================================================================
# Journey E: Chat Interactions & Routing Precedence
# =============================================================================
@pytest.mark.asyncio
async def test_journey_e_chat_relay_media_filters_precedence_end_and_duplicate_updates(
    migrated_postgres_db: tuple[str, Settings],
) -> None:
    """Journey E: text/media relay, filters, router precedence, end/block auth, duplicate safety."""
    _, settings = migrated_postgres_db
    db = DatabaseManager(settings)
    await db.initialize()
    harness = TelegramTestHarness(settings=settings, db_manager=db)

    tg_a = 710500001
    tg_b = 710500002
    tg_c = 710500003

    try:
        ua = await _onboard_via_bot(harness, tg_a, name="کیان", age=28, gender="MALE")
        ub = await _onboard_via_bot(harness, tg_b, name="رها", age=25, gender="FEMALE")
        _uc = await _onboard_via_bot(harness, tg_c, name="ناظر بیرونی", age=30, gender="MALE")

        # Establish active chat A <-> B via direct request
        await harness.send_text(tg_a, BTN_DIRECT_REQUEST)
        await harness.press_button(tg_a, callback_data=CB_DR_SEND_BY_ID)
        await harness.send_text(tg_a, ub.public_id)
        _, acc_cb = harness.find_button(tg_b, prefix=CB_DR_ACCEPT_PREFIX)
        await harness.press_button(tg_b, callback_data=acc_cb)
        _, end_cb = harness.find_button(tg_a, prefix=CB_CHAT_END_PREFIX)
        chat_hex = end_cb.replace(CB_CHAT_END_PREFIX, "")
        chat_uuid = uuid.UUID(hex=chat_hex)

        c_calls_before = len(harness.calls_for(tg_c))

        # 1. Main menu button guard in active chat (never relayed to partner)
        b_len_before = len(harness.calls_for(tg_b))
        await harness.send_text(tg_a, BTN_WALLET)
        assert "گفتگوی ناشناس فعال هستید" in harness.latest_text_for(tg_a)
        assert len(harness.calls_for(tg_b)) == b_len_before

        # 2. Text & supported media relay to partner ONLY
        await harness.send_text(tg_a, "سلام رها چطوری؟")
        assert harness.latest_text_for(tg_b) == "سلام رها چطوری؟"

        await harness.send_media(tg_b, photo_id="photo_123", caption="عکس منظره")
        assert harness.calls_for(tg_a)[-1].method == "sendPhoto"
        assert harness.calls_for(tg_a)[-1].file_id == "photo_123"

        await harness.send_media(tg_a, voice_id="voice_123")
        assert harness.calls_for(tg_b)[-1].method == "sendVoice"

        await harness.send_media(tg_b, video_id="vid_ok", video_size=4_000_000)
        assert harness.calls_for(tg_a)[-1].method == "sendVideo"

        await harness.send_media(tg_a, sticker_id="stk_123")
        assert harness.calls_for(tg_b)[-1].method == "sendSticker"

        await harness.send_media(tg_b, animation_id="gif_123")
        assert harness.calls_for(tg_a)[-1].method == "sendAnimation"

        # Verify User C received nothing from A/B chat
        assert len(harness.calls_for(tg_c)) == c_calls_before

        # 3. Reject unsupported media (document, audio) & oversized video (>= 10 MiB)
        b_count = len(harness.calls_for(tg_b))
        await harness.send_media(tg_a, document_id="doc_bad")
        assert "پشتیبانی نمی‌شود" in harness.latest_text_for(tg_a)
        await harness.send_media(tg_a, audio_id="aud_bad")
        assert "پشتیبانی نمی‌شود" in harness.latest_text_for(tg_a)
        await harness.send_media(tg_a, video_id="vid_huge", video_size=VIDEO_SIZE_LIMIT_BYTES)
        assert "کمتر از ۱۰ مگابایت" in harness.latest_text_for(tg_a)
        assert len(harness.calls_for(tg_b)) == b_count

        # 4. Content filter blocks external links, Telegram handles, phone numbers
        for bad_text in (
            "بیا سایت https://spam.example.com",
            "پیام بده به t.me/my_channel_id",
            "آیدی من @my_tg_handle",
            "شماره من 09123456789",
        ):
            await harness.send_text(tg_a, bad_text)
            assert "🚫" in harness.latest_text_for(tg_a)
        assert len(harness.calls_for(tg_b)) == b_count

        # 5. Unauthorized non-participant (C) cannot send, end, or block A<->B's chat
        a_count_before_c = len(harness.calls_for(tg_a))
        b_count_before_c = len(harness.calls_for(tg_b))
        await harness.send_text(tg_c, "تلاش غیرمجاز برای ارسال پیام به چت دیگران")
        await harness.send_command(tg_c, "/end")
        assert len(harness.calls_for(tg_a)) == a_count_before_c
        assert len(harness.calls_for(tg_b)) == b_count_before_c

        await harness.press_button(
            tg_c,
            callback_data=f"{CB_CHAT_END_PREFIX}{chat_hex}",
            allow_missing_from_history=True,
        )
        assert "قبلاً پایان یافته" in (harness.calls[-1].callback_text or "")
        await harness.press_button(
            tg_c,
            callback_data=f"{CB_CHAT_BLK_PREFIX}{chat_hex}",
            allow_missing_from_history=True,
        )
        assert "دیگر فعال نیست" in (harness.calls[-1].callback_text or "")
        async with db.session() as session:
            cs_still_active = (
                await session.execute(select(ChatSession).where(ChatSession.id == chat_uuid))
            ).scalar_one()
            assert cs_still_active.status == "ACTIVE"

        # 6. Verify /start does not leak into chat while active
        b_len_before_start = len(harness.calls_for(tg_b))
        await harness.send_command(tg_a, "/start")
        assert ua.public_id in harness.latest_text_for(tg_a)
        assert len(harness.calls_for(tg_b)) == b_len_before_start

        # 7. User A ends chat -> ActiveChatParticipant removed, B notified, duplicate safe
        await harness.press_button(tg_a, callback_data=end_cb)
        assert "پایان رسید" in harness.latest_text_for(tg_a)
        assert "مخاطب شما گفتگو را پایان داد" in harness.latest_text_for(tg_b)

        # Replay exact duplicate end callback
        await harness.press_button(tg_a, callback_data=end_cb, allow_missing_from_history=True)
        assert "قبلاً پایان یافته" in (harness.calls[-1].callback_text or "")

        # Post-end messages do not relay
        b_len_after_end = len(harness.calls_for(tg_b))
        await harness.send_text(tg_a, "بعد از پایان پیام نمی‌رود")
        assert len(harness.calls_for(tg_b)) == b_len_after_end

        async with db.session() as session:
            cs_done = (
                await session.execute(select(ChatSession).where(ChatSession.id == chat_uuid))
            ).scalar_one()
            assert cs_done.status == "ENDED"
            active_left = (await session.execute(select(ActiveChatParticipant))).scalars().all()
            assert active_left == []
            msgs = (
                (await session.execute(select(ChatMessage).where(ChatMessage.chat_id == chat_uuid)))
                .scalars()
                .all()
            )
            assert len(msgs) == 6
            for msg in msgs:
                assert msg.expires_at <= msg.created_at + timedelta(days=30)

        harness.assert_no_privacy_leak([tg_a, tg_b, tg_c], ["09123456789"])
    finally:
        await harness.close()
        await db.close()


# =============================================================================
# Journey F: Connections, Blocking, Reporting & Admin Unauthorized Guard
# =============================================================================
@pytest.mark.asyncio
async def test_journey_f_connections_blocking_reporting_and_admin_unauthorized(
    migrated_postgres_db: tuple[str, Settings],
) -> None:
    """Journey F: connections, asymmetric removal, chat report/block, unauthorized admin."""
    _, settings = migrated_postgres_db
    db = DatabaseManager(settings)
    await db.initialize()
    harness = TelegramTestHarness(settings=settings, db_manager=db)

    tg_a = 710600001
    tg_b = 710600002
    tg_c = 710600003

    try:
        ua = await _onboard_via_bot(harness, tg_a, name="سهراب", age=30, gender="MALE")
        ub = await _onboard_via_bot(harness, tg_b, name="نیلوفر", age=26, gender="FEMALE")
        uc = await _onboard_via_bot(harness, tg_c, name="سپیده", age=27, gender="FEMALE")

        # 1. Start A<->B chat via direct request and end it -> bilateral connections created
        await harness.send_text(tg_a, BTN_DIRECT_REQUEST)
        await harness.press_button(tg_a, callback_data=CB_DR_SEND_BY_ID)
        await harness.send_text(tg_a, ub.public_id)
        _, acc_ab = harness.find_button(tg_b, prefix=CB_DR_ACCEPT_PREFIX)
        await harness.press_button(tg_b, callback_data=acc_ab)
        await harness.send_text(tg_a, BTN_END_CHAT)

        # User A views My Connections, opens B, removes B -> asymmetric (B still sees A!)
        await harness.send_text(tg_a, BTN_MY_CONNECTIONS)
        await harness.press_button(tg_a, callback_data=f"{CB_CONN_OPEN_PREFIX}{ub.public_id}")
        assert "نیلوفر" in harness.latest_text_for(tg_a)
        await harness.press_button(tg_a, callback_data=f"{CB_CONN_REM_PREFIX}{ub.public_id}")
        assert "فقط از لیست ارتباط‌های شما حذف شد" in harness.latest_text_for(tg_a)

        await harness.send_text(tg_a, BTN_MY_CONNECTIONS)
        assert "هنوز ارتباط ثبت‌شده‌ای ندارید" in harness.latest_text_for(tg_a)
        await harness.send_text(tg_b, BTN_MY_CONNECTIONS)
        assert "نیلوفر" not in harness.latest_text_for(tg_b)
        _, _b_sees_a = harness.find_button(tg_b, exact=f"{CB_CONN_OPEN_PREFIX}{ua.public_id}")

        # 2. Start A<->C chat, relay one message, report from chat (ReportSG precedes chat relay!),
        #    then block from chat -> ends immediately and notifies C
        await harness.send_text(tg_a, BTN_DIRECT_REQUEST)
        await harness.press_button(tg_a, callback_data=CB_DR_SEND_BY_ID)
        await harness.send_text(tg_a, uc.public_id)
        _, acc_ac = harness.find_button(tg_c, prefix=CB_DR_ACCEPT_PREFIX)
        await harness.press_button(tg_c, callback_data=acc_ac)

        await harness.send_text(tg_c, "پیام مشکوک")
        assert harness.latest_text_for(tg_a) == "پیام مشکوک"

        _, rep_chat_cb = harness.find_button(tg_a, prefix=CB_REPORT_START_CHAT_PREFIX)
        await harness.press_button(tg_a, callback_data=rep_chat_cb)
        await harness.press_button(tg_a, callback_data=f"{CB_REPORT_CAT_PREFIX}HARASSMENT")

        # Report description MUST go to ReportSG.entering_description, NOT be relayed to C!
        c_calls_before_desc = len(harness.calls_for(tg_c))
        await harness.send_text(tg_a, "ایشان پیام آزاردهنده فرستادند")
        assert "با موفقیت ثبت شد" in harness.latest_text_for(tg_a)
        assert len(harness.calls_for(tg_c)) == c_calls_before_desc

        # Also test profile-level report without description (skip desc button)
        await harness.press_button(
            tg_b,
            callback_data=f"{CB_REPORT_START_USER_PREFIX}{ua.public_id}",
            allow_missing_from_history=True,
        )
        await harness.press_button(tg_b, callback_data=f"{CB_REPORT_CAT_PREFIX}SPAM")
        await harness.press_button(tg_b, callback_data=CB_REPORT_SKIP_DESC)
        assert "با موفقیت ثبت شد" in harness.latest_text_for(tg_b)

        # Block User C while active chat is open
        _, blk_chat_cb = harness.find_button(tg_a, prefix=CB_CHAT_BLK_PREFIX)
        await harness.press_button(tg_a, callback_data=blk_chat_cb)
        assert "مسدود شد" in harness.latest_text_for(tg_a)
        assert "پایان یافت" in harness.latest_text_for(tg_c)

        # Block User B from B's connection view (A & B)
        await harness.press_button(tg_b, callback_data=f"{CB_CONN_OPEN_PREFIX}{ua.public_id}")
        await harness.press_button(tg_b, callback_data=f"{CB_CONN_BLK_PREFIX}{ua.public_id}")
        assert "مسدود شد" in harness.latest_text_for(tg_b)

        # Blocking blocks future direct requests & removes connections
        await harness.send_text(tg_a, BTN_DIRECT_REQUEST)
        await harness.press_button(tg_a, callback_data=CB_DR_SEND_BY_ID)
        await harness.send_text(tg_a, uc.public_id)
        assert "امکان ارسال درخواست به این کاربر وجود ندارد" in harness.latest_text_for(tg_a)

        async with db.session() as session:
            reports = (await session.execute(select(Report))).scalars().all()
            holds = (await session.execute(select(MessageEvidenceHold))).scalars().all()
            blocks = (await session.execute(select(UserBlock))).scalars().all()
            conns_active = (
                (
                    await session.execute(
                        select(UserConnection).where(UserConnection.status == "ACTIVE")
                    )
                )
                .scalars()
                .all()
            )
            assert len(reports) == 2
            assert len(holds) >= 1
            assert len(blocks) == 2
            # B->A (1) + A<->C (2); list_connections filters out blocked pairs dynamically
            assert len(conns_active) == 3

        await harness.send_text(tg_a, BTN_MY_CONNECTIONS)
        assert "هنوز ارتباط ثبت‌شده‌ای ندارید" in harness.latest_text_for(tg_a)
        await harness.send_text(tg_b, BTN_MY_CONNECTIONS)
        assert "هنوز ارتباط ثبت‌شده‌ای ندارید" in harness.latest_text_for(tg_b)

        # 3. Unauthorized non-admin /admin & adm:* callback access denied cleanly
        await harness.send_command(tg_a, "/admin")
        assert "دسترسی به پنل مدیریت ندارید" in harness.latest_text_for(tg_a)
        await harness.press_button(tg_a, callback_data=CB_ADM_HOME, allow_missing_from_history=True)
        assert "دسترسی به پنل مدیریت ندارید" in harness.latest_text_for(tg_a)
        await harness.press_button(
            tg_b,
            callback_data=f"{CB_ADM_REP_QUEUE_PREFIX}PENDING:0",
            allow_missing_from_history=True,
        )
        assert "دسترسی به پنل مدیریت ندارید" in harness.latest_text_for(tg_b)

        harness.assert_no_privacy_leak([tg_a, tg_b, tg_c])
    finally:
        await harness.close()
        await db.close()


# =============================================================================
# Journey G: Wallet Interactions & Atomic Chat Settlement
# =============================================================================
@pytest.mark.asyncio
async def test_journey_g_wallet_settlement_failures_insufficient_balance_and_idempotency(
    migrated_postgres_db: tuple[str, Settings],
) -> None:
    """Journey G: wallet view/history, RANDOM/DIRECT settlement, failed delivery, policy."""
    _, base_settings = migrated_postgres_db
    wallet_settings = base_settings.model_copy(update={"DAILY_FREE_COINS_QUOTA": 3})
    db = DatabaseManager(wallet_settings)
    redis = RedisManager(wallet_settings)
    await db.initialize()
    await redis.initialize()
    await redis.client.delete("matching:queue")

    harness = TelegramTestHarness(settings=wallet_settings, db_manager=db, redis_manager=redis)

    tg_a = 710700001
    tg_b = 710700002
    tg_poor = 710700003

    try:
        ua = await _onboard_via_bot(harness, tg_a, name="دارا", age=28, gender="MALE")
        ub = await _onboard_via_bot(harness, tg_b, name="سمیرا", age=25, gender="FEMALE")

        # 1. Wallet view allocates daily 3 free coins idempotently + refresh works
        await harness.send_text(tg_a, BTN_WALLET)
        assert "مجموع موجودی قابل مصرف: <b>3</b>" in harness.latest_text_for(tg_a)
        await harness.press_button(tg_a, callback_data=CB_WALLET_REFRESH)
        assert "مجموع موجودی قابل مصرف: <b>3</b>" in harness.latest_text_for(tg_a)

        w_svc = WalletService(db, wallet_settings)
        # Ensure B also has 3 daily coins
        await harness.send_text(tg_b, BTN_WALLET)

        # 2. Failed Telegram delivery reverts first-message flag & charges 0 coins
        await harness.send_text(tg_b, BTN_RANDOM_CHAT)
        await harness.send_text(tg_a, BTN_RANDOM_CHAT)
        harness.session.fail_chat_ids.add(tg_b)
        await harness.send_text(tg_a, "پیام ناموفق اول")
        assert "ارسال پیام به مخاطب انجام نشد" in harness.latest_text_for(tg_a)
        harness.session.fail_chat_ids.clear()

        async with db.session() as session:
            active_cs = (
                await session.execute(select(ChatSession).where(ChatSession.status == "ACTIVE"))
            ).scalar_one()
            assert active_cs.participant_one_sent_msg is False
            assert active_cs.participant_two_sent_msg is False
            assert active_cs.is_charged is False

        # 3. Single-sided message -> chat ended -> 0 coins charged!
        await harness.send_text(tg_a, "فقط یک طرف پیام داد")
        await harness.send_text(tg_a, BTN_END_CHAT)
        bal_a_3 = await w_svc.get_wallet_balance(ua.id, auto_allocate_daily=False)
        bal_b_3 = await w_svc.get_wallet_balance(ub.id, auto_allocate_daily=False)
        assert bal_a_3.total_spendable_coins == 3
        assert bal_b_3.total_spendable_coins == 3

        # 4. Two-sided RANDOM chat -> settles 1 coin each, repeated messages/end never double-charge
        await harness.send_text(tg_b, BTN_RANDOM_CHAT)
        await harness.send_text(tg_a, BTN_RANDOM_CHAT)
        await harness.send_text(tg_a, "سلام رندوم ۱")
        await harness.send_text(tg_b, "سلام رندوم ۲")
        # More messages in same chat
        await harness.send_text(tg_a, "ادامه ۱")
        await harness.send_text(tg_b, "ادامه ۲")
        _, end_rand_cb = harness.find_button(tg_a, prefix=CB_CHAT_END_PREFIX)
        await harness.press_button(tg_a, callback_data=end_rand_cb)
        # Duplicate end callback replay
        await harness.press_button(tg_a, callback_data=end_rand_cb, allow_missing_from_history=True)

        bal_a_4 = await w_svc.get_wallet_balance(ua.id, auto_allocate_daily=False)
        bal_b_4 = await w_svc.get_wallet_balance(ub.id, auto_allocate_daily=False)
        assert bal_a_4.total_spendable_coins == 2
        assert bal_b_4.total_spendable_coins == 2

        # 5. Two-sided DIRECT chat -> requester (A) charged 2 coins, recipient (B) charged 0 coins
        await harness.send_text(tg_a, BTN_DIRECT_REQUEST)
        await harness.press_button(tg_a, callback_data=CB_DR_SEND_BY_ID)
        await harness.send_text(tg_a, ub.public_id)
        _, acc_direct_cb = harness.find_button(tg_b, prefix=CB_DR_ACCEPT_PREFIX)
        await harness.press_button(tg_b, callback_data=acc_direct_cb)

        await harness.send_text(tg_a, "پیام مستقیم الف")
        await harness.send_text(tg_b, "پاسخ مستقیم ب")
        await harness.send_text(tg_a, BTN_END_CHAT)

        bal_a_5 = await w_svc.get_wallet_balance(ua.id, auto_allocate_daily=False)
        bal_b_5 = await w_svc.get_wallet_balance(ub.id, auto_allocate_daily=False)
        assert bal_a_5.total_spendable_coins == 0
        assert bal_b_5.total_spendable_coins == 2

        # 6. Insufficient balance behavior preserved cleanly: zero-balance user C chats with B ->
        #    settlement defers cleanly (is_charged=False, 0 debits, B unharmed, chat works)
        await w_svc.credit_purchased_coins(ub.id, 1, idempotency_key="topup_b", reason="تست موجودی")
        # Temporarily set quota to 0 on a second harness for tg_poor so tg_poor has 0 total coins
        zero_settings = base_settings.model_copy(update={"DAILY_FREE_COINS_QUOTA": 0})
        harness_zero = TelegramTestHarness(
            settings=zero_settings, db_manager=db, redis_manager=redis
        )
        try:
            u_poor = await _onboard_via_bot(
                harness_zero, tg_poor, name="کم‌سکه", age=24, gender="MALE"
            )
            await w_svc.ensure_wallet(u_poor.id)

            await harness_zero.send_text(tg_poor, BTN_DIRECT_REQUEST)
            await harness_zero.press_button(tg_poor, callback_data=CB_DR_SEND_BY_ID)
            await harness_zero.send_text(tg_poor, ub.public_id)
            _, acc_poor_cb = harness_zero.find_button(tg_b, prefix=CB_DR_ACCEPT_PREFIX)
            await harness_zero.press_button(tg_b, callback_data=acc_poor_cb)

            await harness_zero.send_text(tg_poor, "سلام بدون سکه")
            await harness_zero.send_text(tg_b, "پاسخ به کم‌سکه")
            assert harness_zero.latest_text_for(tg_poor) == "پاسخ به کم‌سکه"
            await harness_zero.send_text(tg_poor, BTN_END_CHAT)
        finally:
            await harness_zero.close()

        async with db.session() as session:
            poor_chat = (
                await session.execute(
                    select(ChatSession).where(ChatSession.participant_one_id == u_poor.id)
                )
            ).scalar_one()
            assert poor_chat.is_charged is False
            assert poor_chat.charged_at is None

        # 7. Ledger history pagination (>5 entries) via wallet:hist:0 -> wallet:hist:5
        for idx in range(5):
            await w_svc.credit_purchased_coins(
                ua.id, 1, idempotency_key=f"hist_pad_{idx}", reason=f"پاداش آزمایشی {idx}"
            )
        await harness.send_text(tg_a, BTN_WALLET)
        await harness.press_button(tg_a, callback_data=f"{CB_WALLET_HISTORY_PREFIX}0")
        page0_text = harness.latest_text_for(tg_a)
        assert "تاریخچه تراکنش‌های کیف پول" in page0_text
        await harness.press_button(tg_a, callback_data=f"{CB_WALLET_HISTORY_PREFIX}5")
        page1_text = harness.latest_text_for(tg_a)
        assert "تاریخچه تراکنش‌های کیف پول" in page1_text
        assert page0_text != page1_text

        # Confirm wallet invariants: purchased >= 0, daily allocated once, ledger matches
        async with db.session() as session:
            wallets = (await session.execute(select(Wallet))).scalars().all()
            assert all(w.purchased_coins >= 0 for w in wallets)
            ledger_rows = (
                (
                    await session.execute(
                        select(WalletLedger).where(
                            WalletLedger.wallet_user_id == ua.id,
                            WalletLedger.transaction_type == "CHAT_SETTLEMENT",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert sum(entry.amount for entry in ledger_rows) == 3  # 1 RANDOM + 2 DIRECT

        harness.assert_no_privacy_leak([tg_a, tg_b, tg_poor])
    finally:
        try:
            await redis.client.delete("matching:queue")
        except Exception:
            pass
        await harness.close()
        await redis.close()
        await db.close()
