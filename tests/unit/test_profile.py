"""Unit tests for user profile validation, service rules, and Telegram bot handlers."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.types import User as TelegramUser

from src.app.bot.handlers.profile import (
    format_profile_view,
    format_review_summary,
    handle_cancel_edit_callback,
    handle_creation_age,
    handle_creation_gender,
    handle_creation_name,
    handle_edit_field_trigger,
    handle_profile_menu_entry,
    handle_skip_bio_callback,
    handle_skip_city_callback,
)
from src.app.bot.keyboards.profile import (
    CB_EDIT_NAME,
    CB_GENDER_PREFIX,
)
from src.app.bot.states import ProfileCreationSG, ProfileEditSG
from src.app.modules.profiles.service import (
    compute_completion_status,
    validate_age,
    validate_bio,
    validate_city,
    validate_display_name,
    validate_gender,
)
from src.app.modules.users.models import User, UserProfile


def create_mock_message(user_id: int = 990001, text: str = "") -> AsyncMock:
    """Helper creating a fully async-mocked Telegram Message."""
    msg = AsyncMock(spec=Message)
    msg.from_user = TelegramUser(id=user_id, is_bot=False, first_name="Test")
    msg.text = text
    msg.answer = AsyncMock()
    msg.edit_reply_markup = AsyncMock()
    msg.edit_text = AsyncMock()
    return msg


def create_mock_callback(user_id: int = 990001, data: str = "") -> AsyncMock:
    """Helper creating a fully async-mocked Telegram CallbackQuery."""
    cb = AsyncMock(spec=CallbackQuery)
    cb.from_user = TelegramUser(id=user_id, is_bot=False, first_name="Test")
    cb.data = data
    cb.answer = AsyncMock()
    cb.message = create_mock_message(user_id=user_id)
    return cb


# =============================================================================
# 1. Validation Helper Unit Tests
# =============================================================================
def test_valid_display_name_accepted() -> None:
    """Verify display names between 2 and 50 characters are accepted."""
    assert validate_display_name("Ali") == "Ali"
    assert validate_display_name("  رضا  ") == "رضا"
    assert validate_display_name("A" * 50) == "A" * 50


def test_invalid_display_name_rejected() -> None:
    """Verify empty, short (<2), or long (>50) display names raise ValueError."""
    with pytest.raises(ValueError, match="نام نمایشی الزامی است"):
        validate_display_name("")
    with pytest.raises(ValueError, match="نام نمایشی الزامی است"):
        validate_display_name(None)
    with pytest.raises(ValueError, match="باید بین ۲ تا ۵۰ کاراکتر باشد"):
        validate_display_name("A")
    with pytest.raises(ValueError, match="باید بین ۲ تا ۵۰ کاراکتر باشد"):
        validate_display_name("A" * 51)


def test_age_17_rejected() -> None:
    """Verify age 17 raises ValueError."""
    with pytest.raises(ValueError, match="باید بین ۱۸ تا ۹۹ سال باشد"):
        validate_age(17)


def test_age_18_accepted() -> None:
    """Verify age 18 is accepted."""
    assert validate_age(18) == 18
    assert validate_age("18") == 18


def test_age_99_accepted() -> None:
    """Verify age 99 is accepted."""
    assert validate_age(99) == 99
    assert validate_age("99") == 99


def test_age_100_rejected() -> None:
    """Verify age 100 raises ValueError."""
    with pytest.raises(ValueError, match="باید بین ۱۸ تا ۹۹ سال باشد"):
        validate_age(100)


def test_non_numeric_age_rejected() -> None:
    """Verify non-numeric string age raises ValueError."""
    with pytest.raises(ValueError, match="عدد معتبر"):
        validate_age("eighteen")
    with pytest.raises(ValueError, match="سن الزامی است"):
        validate_age(None)


def test_each_gender_option_works() -> None:
    """Verify MALE, FEMALE, NON_BINARY, OTHER choices pass validation."""
    for gender in ["MALE", "FEMALE", "NON_BINARY", "OTHER"]:
        assert validate_gender(gender) == gender
        assert validate_gender(gender.lower()) == gender


def test_invalid_gender_rejected() -> None:
    """Verify unknown gender strings raise ValueError."""
    with pytest.raises(ValueError, match="جنسیت انتخاب‌شده معتبر نمی‌باشد"):
        validate_gender("INVALID_GENDER")


def test_city_can_be_skipped() -> None:
    """Verify None or empty city string returns None without error."""
    assert validate_city(None) is None
    assert validate_city("") is None
    assert validate_city("   ") is None
    assert validate_city("Tehran") == "Tehran"


def test_city_long_rejected() -> None:
    """Verify city over 100 characters raises ValueError."""
    with pytest.raises(ValueError, match="بیش از ۱۰۰ کاراکتر"):
        validate_city("A" * 101)


def test_bio_can_be_skipped() -> None:
    """Verify None or empty bio string returns None without error."""
    assert validate_bio(None) is None
    assert validate_bio("") is None
    assert validate_bio("Hello world") == "Hello world"


def test_bio_long_rejected() -> None:
    """Verify bio over 500 characters raises ValueError."""
    with pytest.raises(ValueError, match="بیش از ۵۰۰ کاراکتر"):
        validate_bio("A" * 501)


def test_profile_completion_status_calculation() -> None:
    """Verify completion_status is COMPLETED only when required fields are present."""
    assert compute_completion_status("Ali", 25, "MALE") == "COMPLETED"
    assert compute_completion_status("", 25, "MALE") == "INCOMPLETE"
    assert compute_completion_status("Ali", None, "MALE") == "INCOMPLETE"
    assert compute_completion_status("Ali", 25, None) == "INCOMPLETE"


# =============================================================================
# 2. Handler Unit Tests with Mocks
# =============================================================================
@pytest.mark.asyncio
async def test_unregistered_user_is_denied_profile_access() -> None:
    """Verify unregistered user cannot access profile and sees age declaration prompt."""
    mock_db = MagicMock()
    mock_message = create_mock_message(user_id=990001)
    mock_state = AsyncMock(spec=FSMContext)

    with pytest.MonkeyPatch.context() as m:
        m.setattr(
            "src.app.bot.handlers.profile.UserService.get_user_by_telegram_id",
            AsyncMock(return_value=None),
        )
        await handle_profile_menu_entry(mock_message, mock_state, db_manager=mock_db)

    mock_state.clear.assert_awaited_once()
    mock_message.answer.assert_awaited_once()
    args, _kwargs = mock_message.answer.call_args
    assert "شرایط سنی ۱۸+ سال" in args[0]


@pytest.mark.asyncio
async def test_registered_user_without_profile_starts_profile_creation() -> None:
    """Verify registered user without profile receives prompt to create profile."""
    mock_db = MagicMock()
    mock_message = create_mock_message(user_id=990002)
    mock_state = AsyncMock(spec=FSMContext)

    mock_user = MagicMock(spec=User)
    mock_user.age_18_plus_declared = True

    with pytest.MonkeyPatch.context() as m:
        m.setattr(
            "src.app.bot.handlers.profile.UserService.get_user_by_telegram_id",
            AsyncMock(return_value=mock_user),
        )
        m.setattr(
            "src.app.bot.handlers.profile.ProfileService.get_profile_by_telegram_id",
            AsyncMock(return_value=None),
        )
        await handle_profile_menu_entry(mock_message, mock_state, db_manager=mock_db)

    mock_message.answer.assert_awaited_once()
    args, _kwargs = mock_message.answer.call_args
    assert "شما هنوز پروفایلی ایجاد نکرده‌اید" in args[0]


@pytest.mark.asyncio
async def test_existing_profile_is_displayed_correctly() -> None:
    """Verify existing user profile formatted text is displayed with edit options."""
    mock_db = MagicMock()
    mock_message = create_mock_message(user_id=990003)
    mock_state = AsyncMock(spec=FSMContext)

    mock_user = MagicMock(spec=User)
    mock_user.age_18_plus_declared = True

    mock_profile = UserProfile(
        user_id=1,
        age_18_plus_declared=True,
        age=28,
        display_name="سارا",
        gender="FEMALE",
        city="شیراز",
        bio="سلام به همه",
        completion_status="COMPLETED",
        visibility="PUBLIC",
    )

    with pytest.MonkeyPatch.context() as m:
        m.setattr(
            "src.app.bot.handlers.profile.UserService.get_user_by_telegram_id",
            AsyncMock(return_value=mock_user),
        )
        m.setattr(
            "src.app.bot.handlers.profile.ProfileService.get_profile_by_telegram_id",
            AsyncMock(return_value=mock_profile),
        )
        await handle_profile_menu_entry(mock_message, mock_state, db_manager=mock_db)

    mock_message.answer.assert_awaited_once()
    args, _kwargs = mock_message.answer.call_args
    assert "سارا" in args[0]
    assert "28" in args[0] or "۲۸" in args[0]
    assert "شیراز" in args[0]


@pytest.mark.asyncio
async def test_creation_wizard_step_by_step_and_skip() -> None:
    """Test full creation wizard sequence with name, age, gender, city skip, bio skip."""
    mock_state = AsyncMock(spec=FSMContext)
    mock_message = create_mock_message()

    # 1. Name input
    mock_message.text = "علی"
    await handle_creation_name(mock_message, mock_state)
    mock_state.update_data.assert_awaited_with(display_name="علی")
    mock_state.set_state.assert_awaited_with(ProfileCreationSG.waiting_for_age)

    # 2. Age input
    mock_message.text = "22"
    await handle_creation_age(mock_message, mock_state)
    mock_state.update_data.assert_awaited_with(age=22)
    mock_state.set_state.assert_awaited_with(ProfileCreationSG.waiting_for_gender)

    # 3. Gender callback query
    mock_callback = create_mock_callback(data=f"{CB_GENDER_PREFIX}MALE")
    await handle_creation_gender(mock_callback, mock_state)
    mock_callback.answer.assert_awaited_once()

    # 4. Skip city callback query
    mock_callback_city = create_mock_callback()
    await handle_skip_city_callback(mock_callback_city, mock_state)
    mock_callback_city.answer.assert_awaited_once()

    # 5. Skip bio callback query
    mock_callback_bio = create_mock_callback()
    mock_state.get_data.return_value = {
        "display_name": "علی",
        "age": 22,
        "gender": "MALE",
        "city": None,
        "bio": None,
    }
    await handle_skip_bio_callback(mock_callback_bio, mock_state)
    mock_callback_bio.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_editable_fields_and_cancel_edit() -> None:
    """Verify field edit trigger and cancel edit leaving profile unchanged."""
    mock_state = AsyncMock(spec=FSMContext)
    mock_callback = create_mock_callback(user_id=990004, data=CB_EDIT_NAME)

    mock_profile = UserProfile(
        user_id=1,
        age_18_plus_declared=True,
        age=30,
        display_name="رضا",
        gender="MALE",
        city="اصفهان",
        completion_status="COMPLETED",
    )

    # Trigger edit display name
    await handle_edit_field_trigger(mock_callback, mock_state)
    mock_state.set_state.assert_awaited_with(ProfileEditSG.editing_display_name)

    # Cancel edit
    with pytest.MonkeyPatch.context() as m:
        m.setattr(
            "src.app.bot.handlers.profile.ProfileService.get_profile_by_telegram_id",
            AsyncMock(return_value=mock_profile),
        )
        await handle_cancel_edit_callback(mock_callback, mock_state, db_manager=MagicMock())

    mock_callback.answer.assert_awaited_with("ویرایش لغو شد.")
    mock_state.clear.assert_awaited()


@pytest.mark.asyncio
async def test_database_errors_do_not_leak_internal_details() -> None:
    """Verify database exceptions show user-friendly Persian error message without leaking SQL."""
    mock_db = MagicMock()
    mock_message = create_mock_message(user_id=990005)
    mock_state = AsyncMock(spec=FSMContext)

    with pytest.MonkeyPatch.context() as m:
        m.setattr(
            "src.app.bot.handlers.profile.UserService.get_user_by_telegram_id",
            AsyncMock(side_effect=RuntimeError("FATAL: connection lost to PostgreSQL 16 server")),
        )
        await handle_profile_menu_entry(mock_message, mock_state, db_manager=mock_db)

    mock_message.answer.assert_awaited_once()
    args, _kwargs = mock_message.answer.call_args
    assert "PostgreSQL" not in args[0]
    assert "FATAL" not in args[0]
    assert "خطایی رخ داده است" in args[0]


def test_format_profile_view_escapes_html_injection() -> None:
    """Verify malicious HTML tags in display_name, city, and bio are escaped literally."""
    malicious_profile = UserProfile(
        user_id=1,
        age_18_plus_declared=True,
        age=25,
        display_name="<b>Hacker</b> & <script>",
        gender="MALE",
        city="<img src=x onerror=alert(1)>",
        bio="Hello <i>world</i> & 'quotes' \"double\"",
        completion_status="COMPLETED",
        visibility="PUBLIC",
    )

    rendered = format_profile_view(malicious_profile, is_new=False)

    # Must contain escaped entities
    assert "&lt;b&gt;Hacker&lt;/b&gt; &amp; &lt;script&gt;" in rendered
    assert "&lt;img src=x onerror=alert(1)&gt;" in rendered
    assert "&lt;i&gt;world&lt;/i&gt; &amp; &#x27;quotes&#x27; &quot;double&quot;" in rendered

    # Must NOT contain raw unescaped user tags
    assert "<b>Hacker</b>" not in rendered
    assert "<script>" not in rendered
    assert "<img src=x onerror=alert(1)>" not in rendered
    assert "<i>world</i>" not in rendered

    # Trusted structure HTML tags should remain intact
    assert "<b>نام نمایشی:</b>" in rendered
    assert "<b>سن:</b>" in rendered
    assert "<b>شهر:</b>" in rendered
    assert "<b>درباره من:</b>" in rendered


def test_format_review_summary_escapes_html_injection() -> None:
    """Verify malicious HTML tags in review summary dictionary are escaped literally."""
    data = {
        "display_name": "<b>BoldName</b>",
        "age": 30,
        "gender": "FEMALE",
        "city": "Tehran <city>",
        "bio": "Bio & <span style='color:red'>alert</span>",
    }

    summary = format_review_summary(data)

    assert "&lt;b&gt;BoldName&lt;/b&gt;" in summary
    assert "Tehran &lt;city&gt;" in summary
    assert "Bio &amp; &lt;span style=&#x27;color:red&#x27;&gt;alert&lt;/span&gt;" in summary

    assert "<b>BoldName</b>" not in summary
    assert "<city>" not in summary
    assert "style='color:red'" not in summary

    # Trusted structure HTML tags should remain intact
    assert "<b>پیش‌نمایش و تأیید نهایی پروفایل</b>" in summary
