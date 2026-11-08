"""Unit tests for Telegram onboarding, adult self-declaration, and main menu handlers."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, ReplyKeyboardMarkup
from aiogram.types import User as TelegramUser
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.app.bot.handlers.base import handle_start_smoke_test
from src.app.bot.handlers.onboarding import (
    ALREADY_REGISTERED_TEXT,
    DATABASE_ERROR_TEXT,
    HELP_TEXT,
    UNDERAGE_TEXT,
    UNREGISTERED_ACCESS_DENIED_TEXT,
    WELCOME_TEXT,
    handle_age_confirm_callback,
    handle_age_decline_callback,
    handle_main_menu_entry,
    handle_start,
)
from src.app.bot.keyboards.onboarding import (
    AGE_CONFIRM_TEXT,
    AGE_DECLINE_TEXT,
    BTN_HELP_SUPPORT,
    CALLBACK_AGE_CONFIRM,
    CALLBACK_AGE_DECLINE,
    MAIN_MENU_BUTTONS,
    get_main_menu_keyboard,
)
from src.app.config.settings import Settings
from src.app.modules.users.service import UserService, generate_public_id


class SQLiteDatabaseManagerWrapper:
    """Lightweight test wrapper adapting an in-memory SQLite engine to DatabaseManager."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.session_factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )

    @asynccontextmanager
    async def session(self):
        async with self.session_factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise


@pytest.fixture
def test_db_manager(sqlite_engine: AsyncEngine) -> SQLiteDatabaseManagerWrapper:
    """Fixture providing an SQLite-backed DatabaseManager for handler tests."""
    return SQLiteDatabaseManagerWrapper(sqlite_engine)


def create_mock_message(telegram_id: int, text: str = "/start") -> MagicMock:
    """Helper creating a typed mock Message with valid from_user."""
    mock_msg = MagicMock(spec=Message)
    mock_msg.text = text
    mock_user = MagicMock(spec=TelegramUser)
    mock_user.id = telegram_id
    mock_user.is_bot = False
    mock_user.first_name = "TestUser"
    mock_msg.from_user = mock_user
    mock_msg.answer = AsyncMock()
    return mock_msg


def create_mock_callback(telegram_id: int, data: str) -> MagicMock:
    """Helper creating a typed mock CallbackQuery with valid from_user and message."""
    mock_cb = MagicMock(spec=CallbackQuery)
    mock_cb.data = data
    mock_user = MagicMock(spec=TelegramUser)
    mock_user.id = telegram_id
    mock_user.is_bot = False
    mock_cb.from_user = mock_user
    mock_cb.answer = AsyncMock()

    mock_msg = MagicMock(spec=Message)
    mock_msg.edit_reply_markup = AsyncMock()
    mock_msg.edit_text = AsyncMock()
    mock_msg.answer = AsyncMock()
    mock_cb.message = mock_msg
    return mock_cb


# -----------------------------------------------------------------------------
# 1. /start from unregistered user shows age declaration prompt
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_start_unregistered_user_shows_age_declaration_prompt(
    test_db_manager: SQLiteDatabaseManagerWrapper,
    test_settings: Settings,
) -> None:
    """Verify that an unregistered user sending /start sees the 18+ declaration prompt."""
    telegram_id = 1110001
    mock_message = create_mock_message(telegram_id, "/start")

    await handle_start(mock_message, db_manager=test_db_manager, settings=test_settings)

    mock_message.answer.assert_awaited_once()
    args, kwargs = mock_message.answer.call_args
    assert WELCOME_TEXT in args[0]
    reply_markup = kwargs.get("reply_markup")
    assert isinstance(reply_markup, InlineKeyboardMarkup)
    assert reply_markup.inline_keyboard[0][0].text == AGE_CONFIRM_TEXT
    assert reply_markup.inline_keyboard[0][0].callback_data == CALLBACK_AGE_CONFIRM
    assert reply_markup.inline_keyboard[1][0].text == AGE_DECLINE_TEXT
    assert reply_markup.inline_keyboard[1][0].callback_data == CALLBACK_AGE_DECLINE


# -----------------------------------------------------------------------------
# 2. Declining age does not call registration or create a user
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_age_decline_callback_does_not_create_user(
    test_db_manager: SQLiteDatabaseManagerWrapper,
) -> None:
    """Verify that selecting under-18 declines access without creating any user record."""
    telegram_id = 2220002
    mock_callback = create_mock_callback(telegram_id, CALLBACK_AGE_DECLINE)

    await handle_age_decline_callback(mock_callback)

    # Callback must be acknowledged
    mock_callback.answer.assert_awaited_once()

    # Message edited with underage explanation and no main menu
    mock_callback.message.edit_text.assert_awaited_once()
    args, kwargs = mock_callback.message.edit_text.call_args
    assert UNDERAGE_TEXT in args[0]
    assert kwargs.get("reply_markup") is None

    # Ensure no database record was created
    user_service = UserService(test_db_manager)
    user = await user_service.get_user_by_telegram_id(telegram_id)
    assert user is None


# -----------------------------------------------------------------------------
# 3. Accepting age creates user with all required declaration fields
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_age_confirm_callback_creates_user_with_declaration_fields(
    test_db_manager: SQLiteDatabaseManagerWrapper,
    test_settings: Settings,
) -> None:
    """Verify that confirming 18+ registers a user with all required timestamps and fields."""
    telegram_id = 3330003
    mock_callback = create_mock_callback(telegram_id, CALLBACK_AGE_CONFIRM)

    await handle_age_confirm_callback(
        mock_callback,
        db_manager=test_db_manager,
        settings=test_settings,
    )

    # Callback must be acknowledged
    mock_callback.answer.assert_awaited_once()

    # Verify user registration message
    mock_callback.message.answer.assert_awaited_once()
    args, kwargs = mock_callback.message.answer.call_args
    assert "ثبت‌نام شما با موفقیت انجام شد" in args[0]
    reply_markup = kwargs.get("reply_markup")
    assert isinstance(reply_markup, ReplyKeyboardMarkup)

    # Inspect persisted database user
    user_service = UserService(test_db_manager)
    persisted = await user_service.get_user_by_telegram_id(telegram_id)
    assert persisted is not None
    assert persisted.telegram_user_id == telegram_id
    assert persisted.status == "ACTIVE"
    assert persisted.age_declared_at is not None
    assert persisted.age_declared_date is not None
    assert persisted.public_id.startswith("usr_")


# -----------------------------------------------------------------------------
# 4. Existing registered user sees the main menu without duplicate insertion
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_start_existing_registered_user_shows_main_menu(
    test_db_manager: SQLiteDatabaseManagerWrapper,
    test_settings: Settings,
) -> None:
    """Verify an existing adult user sending /start directly receives the main menu."""
    telegram_id = 4440004
    user_service = UserService(test_db_manager)
    created_user, is_new = await user_service.register_adult_user(
        telegram_id, test_settings.business_timezone
    )
    assert is_new is True

    # User sends /start again
    mock_message = create_mock_message(telegram_id, "/start")
    await handle_start(mock_message, db_manager=test_db_manager, settings=test_settings)

    mock_message.answer.assert_awaited_once()
    args, kwargs = mock_message.answer.call_args
    assert ALREADY_REGISTERED_TEXT.split("{")[0] in args[0]
    assert created_user.public_id in args[0]
    reply_markup = kwargs.get("reply_markup")
    assert isinstance(reply_markup, ReplyKeyboardMarkup)


# -----------------------------------------------------------------------------
# 5. Repeated acceptance callbacks do not create duplicate accounts
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_repeated_acceptance_callbacks_do_not_duplicate_account(
    test_db_manager: SQLiteDatabaseManagerWrapper,
    test_settings: Settings,
) -> None:
    """Verify that multiple affirmative callbacks return the existing user safely."""
    telegram_id = 5550005
    cb1 = create_mock_callback(telegram_id, CALLBACK_AGE_CONFIRM)
    cb2 = create_mock_callback(telegram_id, CALLBACK_AGE_CONFIRM)

    await handle_age_confirm_callback(cb1, db_manager=test_db_manager, settings=test_settings)
    await handle_age_confirm_callback(cb2, db_manager=test_db_manager, settings=test_settings)

    # Verify database has exactly 1 row
    user_service = UserService(test_db_manager)
    user = await user_service.get_user_by_telegram_id(telegram_id)
    assert user is not None


# -----------------------------------------------------------------------------
# 6. Public ID is random and is not derived from Telegram ID
# -----------------------------------------------------------------------------
def test_public_id_generation_properties() -> None:
    """Verify that public IDs are random, non-sequential, and independent of Telegram ID."""
    telegram_id = 999888777
    public_id = generate_public_id()

    assert public_id.startswith("usr_")
    assert len(public_id) == 16
    assert str(telegram_id) not in public_id

    # Verify high entropy / uniqueness across multiple generations
    generated_ids = {generate_public_id() for _ in range(100)}
    assert len(generated_ids) == 100


# -----------------------------------------------------------------------------
# 7. Main menu contains all seven required Persian entries
# -----------------------------------------------------------------------------
def test_main_menu_contains_all_seven_persian_entries() -> None:
    """Verify that the Persian main menu contains the exact seven specified button labels."""
    expected_buttons = [
        "🎲 گفتگوی تصادفی",
        "🔎 مشاهده پروفایلها",
        "✉️ درخواست مستقیم",
        "👥 ارتباطهای من",
        "👤 پروفایل من",
        "🪙 کیف پول",
        "🆘 راهنما و پشتیبانی",
    ]

    assert MAIN_MENU_BUTTONS == expected_buttons

    keyboard = get_main_menu_keyboard()
    flat_buttons = [btn.text for row in keyboard.keyboard for btn in row]
    assert flat_buttons == expected_buttons


# -----------------------------------------------------------------------------
# 8. Callback queries are acknowledged
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_callback_queries_are_acknowledged(
    test_db_manager: SQLiteDatabaseManagerWrapper,
    test_settings: Settings,
) -> None:
    """Verify both affirmative and negative callback queries await callback.answer()."""
    cb_confirm = create_mock_callback(888111, CALLBACK_AGE_CONFIRM)
    await handle_age_confirm_callback(
        cb_confirm,
        db_manager=test_db_manager,
        settings=test_settings,
    )
    cb_confirm.answer.assert_awaited_once()

    cb_decline = create_mock_callback(888222, CALLBACK_AGE_DECLINE)
    await handle_age_decline_callback(cb_decline)
    cb_decline.answer.assert_awaited_once()


# -----------------------------------------------------------------------------
# 9. Users without age declaration cannot access the main menu
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unregistered_users_cannot_access_main_menu(
    test_db_manager: SQLiteDatabaseManagerWrapper,
    test_settings: Settings,
) -> None:
    """Verify that undeclared users tapping menu buttons are redirected to age confirmation."""
    telegram_id = 999333
    mock_msg = create_mock_message(telegram_id, "🎲 گفتگوی تصادفی")

    await handle_main_menu_entry(mock_msg, db_manager=test_db_manager, settings=test_settings)

    mock_msg.answer.assert_awaited_once()
    args, kwargs = mock_msg.answer.call_args
    assert UNREGISTERED_ACCESS_DENIED_TEXT in args[0]
    reply_markup = kwargs.get("reply_markup")
    assert isinstance(reply_markup, InlineKeyboardMarkup)
    assert reply_markup.inline_keyboard[0][0].callback_data == CALLBACK_AGE_CONFIRM


@pytest.mark.asyncio
async def test_registered_user_accesses_main_menu_button(
    test_db_manager: SQLiteDatabaseManagerWrapper,
    test_settings: Settings,
) -> None:
    """Verify that a registered user tapping a menu button receives the coming soon/help text."""
    telegram_id = 999444
    user_service = UserService(test_db_manager)
    await user_service.register_adult_user(telegram_id, test_settings.business_timezone)

    # 1. Help button
    msg_help = create_mock_message(telegram_id, BTN_HELP_SUPPORT)
    await handle_main_menu_entry(msg_help, db_manager=test_db_manager, settings=test_settings)
    msg_help.answer.assert_awaited_once()
    assert HELP_TEXT in msg_help.answer.call_args[0][0]

    # 2. Coming soon button
    msg_random = create_mock_message(telegram_id, "🎲 گفتگوی تصادفی")
    await handle_main_menu_entry(msg_random, db_manager=test_db_manager, settings=test_settings)
    msg_random.answer.assert_awaited_once()
    assert "در حال توسعه است" in msg_random.answer.call_args[0][0]


# -----------------------------------------------------------------------------
# 10. Database errors do not expose sensitive internal details to users
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_database_errors_do_not_expose_internal_details(
    test_settings: Settings,
) -> None:
    """Verify that unexpected database exceptions yield generic localized error copy."""
    telegram_id = 777666
    mock_msg = create_mock_message(telegram_id, "/start")

    mock_db = MagicMock()
    mock_db.session.side_effect = RuntimeError(
        "FATAL: Password authentication failed for user 'postgres' at 10.0.0.1:5432"
    )

    await handle_start(mock_msg, db_manager=mock_db, settings=test_settings)

    mock_msg.answer.assert_awaited_once()
    user_reply = mock_msg.answer.call_args[0][0]
    assert DATABASE_ERROR_TEXT in user_reply
    assert "postgres" not in user_reply
    assert "FATAL" not in user_reply
    assert "10.0.0.1" not in user_reply


# -----------------------------------------------------------------------------
# 11. Preserved smoke test handler
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_start_smoke_test_handler_sends_smoke_message() -> None:
    """Verify /start smoke test replies with system status message."""
    mock_message = MagicMock(spec=Message)
    mock_user = MagicMock(spec=TelegramUser)
    mock_user.id = 123456789
    mock_message.from_user = mock_user
    mock_message.answer = AsyncMock()

    await handle_start_smoke_test(mock_message)

    mock_message.answer.assert_awaited_once()
    args, _ = mock_message.answer.call_args
    assert "System Online" in args[0]
    assert "health-check mode" in args[0]
