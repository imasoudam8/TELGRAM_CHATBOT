"""Unit tests for privacy/safety content filtering and discovery preference validation."""

from datetime import UTC, datetime, timedelta

import pytest

from src.app.modules.chat.content_filter import normalize_digits, validate_message_text
from src.app.modules.discovery.schemas import (
    PublicProfileDTO,
    UserPreferencesDTO,
    format_relative_activity,
)
from src.app.modules.discovery.service import (
    profile_matches_preferences,
    validate_preferences_inputs,
)
from src.app.modules.matching.service import is_mutual_match


def test_normalize_persian_and_arabic_digits() -> None:
    """Verify Persian and Arabic digits normalize cleanly to ASCII."""
    assert normalize_digits("۰۹۱۲۳۴۵۶۷۸۹") == "09123456789"
    assert normalize_digits("٠١٢٣٤٥٦٧٨٩") == "0123456789"


@pytest.mark.parametrize(
    "blocked_text",
    [
        "بیا تلگرام t.me/testuser",
        "لینک کانال https://example.com/chat",
        "سایت http://mysite.ir ببین",
        "ادرس www.google.com",
        "آیدی من @ali_reza123 هست",
        "بیا پیوی @sara_dev",
        "شماره من 09123456789 زنگ بزن",
        "شماره‌ام ۰۹۱۲۳۴۵۶۷۸۹ هست",
        "تماس بگیر 0912-345-6789",
        "واتساپ +98 912 345 6789",
        "شماره خارجی +1 415 555 0199",
        "دامنه خالی example.com رو باز کن",
        "ایمیل من test.user@gmail.com هست",
        "t . me/obfuscated",
    ],
)
def test_content_filter_blocks_links_usernames_domains_and_phones(blocked_text: str) -> None:
    """Verify anti-link/contact filter blocks URLs, Telegram handles, bare domains, and phones."""
    res = validate_message_text(blocked_text)
    assert res.allowed is False
    assert res.violation_type is not None
    assert res.reason_persian is not None


@pytest.mark.parametrize(
    "safe_text",
    [
        "سلام! حالت چطوره؟",
        "امروز هوا خیلی خوبه، ساعت 14:30 قرار داریم.",
        "من ۲۵ سالمه و توی تهران زندگی می‌کنم.",
        "تاریخ تولد نیست ولی امروز 1403/08/22 هست.",
        "قیمت این کتاب 250000 تومن بود!",
        "من.تو هم‌نظر هستیم، آفرین.",
        "شماره صفحه 123 رو بخون لطفا",
        "نسخه 2.5 خیلی بهتر کار می‌کنه",
    ],
)
def test_content_filter_allows_harmless_conversation(safe_text: str) -> None:
    """Verify normal Persian/English text, times, dates, decimals, and prices are allowed."""
    res = validate_message_text(safe_text)
    assert res.allowed is True
    assert res.violation_type is None


def test_validate_preferences_inputs_validation() -> None:
    """Verify validation bounds on preference inputs."""
    genders, min_a, max_a, cities, hours, rel = validate_preferences_inputs(
        preferred_genders=["male", "FEMALE", "male"],
        min_age=20,
        max_age=35,
        preferred_cities=[" تهران ", "تهران", "شیراز"],
        recent_activity_hours=24,
        relaxable_fields={"city"},
    )
    assert genders == ["MALE", "FEMALE"]
    assert min_a == 20
    assert max_a == 35
    assert cities == ["تهران", "شیراز"]
    assert hours == 24
    assert rel == {"city"}

    with pytest.raises(ValueError):
        validate_preferences_inputs(min_age=17, max_age=30)

    with pytest.raises(ValueError):
        validate_preferences_inputs(min_age=35, max_age=25)

    with pytest.raises(ValueError):
        validate_preferences_inputs(preferred_genders=["INVALID"])

    with pytest.raises(ValueError):
        validate_preferences_inputs(recent_activity_hours=999)

    with pytest.raises(ValueError):
        validate_preferences_inputs(relaxable_fields={"unknown_field"})


def test_strict_vs_relaxable_matching_logic() -> None:
    """Verify required filters are never relaxed while explicit relaxable filters are respected."""
    now = datetime.now(UTC)
    prefs_a = UserPreferencesDTO(
        user_id=1,
        preferred_genders=["FEMALE"],
        min_age=22,
        max_age=30,
        preferred_cities=["تهران"],
        recent_activity_hours=24,
        relaxable_fields={"city"},  # ONLY city is relaxable; gender/age/activity are strict
    )
    prof_a = PublicProfileDTO(
        user_id=1,
        public_id="usr_a",
        display_name="علی",
        age=26,
        gender="MALE",
        city="تهران",
        bio=None,
        last_activity_at=now,
    )
    prefs_b = UserPreferencesDTO(
        user_id=2,
        preferred_genders=["MALE"],
        min_age=24,
        max_age=35,
        preferred_cities=[],
    )
    prof_b_shiraz = PublicProfileDTO(
        user_id=2,
        public_id="usr_b",
        display_name="سارا",
        age=25,
        gender="FEMALE",
        city="شیراز",
        bio=None,
        last_activity_at=now - timedelta(hours=2),
    )

    # Exact match fails because of city mismatch
    assert (
        profile_matches_preferences(prefs_a, prof_b_shiraz, allow_relaxed=False, now_ts=now)
        is False
    )
    assert (
        is_mutual_match(prefs_a, prof_a, prefs_b, prof_b_shiraz, allow_relaxed=False, now_ts=now)
        is False
    )

    # Relaxed match succeeds ONLY for city (explicitly permitted)
    assert (
        profile_matches_preferences(prefs_a, prof_b_shiraz, allow_relaxed=True, now_ts=now) is True
    )
    assert (
        is_mutual_match(prefs_a, prof_a, prefs_b, prof_b_shiraz, allow_relaxed=True, now_ts=now)
        is True
    )

    # If strict gender doesn't match, even allow_relaxed=True MUST fail
    prof_c_wrong_gender = PublicProfileDTO(
        user_id=3,
        public_id="usr_c",
        display_name="رضا",
        age=25,
        gender="MALE",
        city="شیراز",
        bio=None,
        last_activity_at=now,
    )
    assert (
        profile_matches_preferences(prefs_a, prof_c_wrong_gender, allow_relaxed=True, now_ts=now)
        is False
    )


def test_format_relative_activity_never_claims_telegram_online() -> None:
    """Verify activity wording describes recent bot activity without claiming online presence."""
    now = datetime.now(UTC)
    recent_text = format_relative_activity(now - timedelta(minutes=5), now=now)
    assert "آنلاین" not in recent_text
    assert "فعال" in recent_text

    day_text = format_relative_activity(now - timedelta(hours=10), now=now)
    assert "آنلاین" not in day_text
    assert "۲۴ ساعت" in day_text
