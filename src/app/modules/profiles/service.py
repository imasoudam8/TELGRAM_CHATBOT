"""Profile management service for creating, viewing, and updating user profiles."""

import logging
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.app.infrastructure.database.base import now_utc
from src.app.infrastructure.database.session import DatabaseManager
from src.app.modules.moderation.enforcement import reconcile_user_suspension_in_session
from src.app.modules.users.models import User, UserProfile

logger = logging.getLogger(__name__)

VALID_GENDERS = {"MALE", "FEMALE", "NON_BINARY", "OTHER"}
VALID_VISIBILITIES = {"PUBLIC", "ANONYMOUS", "HIDDEN"}


@dataclass(frozen=True)
class ProfileData:
    """Data container for profile creation or update."""

    display_name: str
    age: int
    gender: str
    city: str | None = None
    bio: str | None = None
    visibility: str = "PUBLIC"


def validate_display_name(name: str | None) -> str:
    """Validate display name: 2-50 characters."""
    if not name or not isinstance(name, str):
        raise ValueError("نام نمایشی الزامی است.")
    cleaned = name.strip()
    if len(cleaned) < 2 or len(cleaned) > 50:
        raise ValueError("نام نمایشی باید بین ۲ تا ۵۰ کاراکتر باشد.")
    return cleaned


def validate_age(age_val: int | str | None) -> int:
    """Validate age: integer between 18 and 99."""
    if age_val is None:
        raise ValueError("سن الزامی است.")
    try:
        age_int = int(age_val)
    except (ValueError, TypeError) as err:
        raise ValueError("سن باید یک عدد معتبر باشد.") from err

    if age_int < 18 or age_int > 99:
        raise ValueError("سن باید بین ۱۸ تا ۹۹ سال باشد.")
    return age_int


def validate_gender(gender_val: str | None) -> str:
    """Validate gender against allowed database values."""
    if not gender_val or gender_val.upper() not in VALID_GENDERS:
        raise ValueError("جنسیت انتخاب‌شده معتبر نمی‌باشد.")
    return gender_val.upper()


def validate_city(city_val: str | None) -> str | None:
    """Validate city: optional, max 100 characters."""
    if not city_val:
        return None
    cleaned = city_val.strip()
    if not cleaned:
        return None
    if len(cleaned) > 100:
        raise ValueError("نام شهر نمی‌تواند بیش از ۱۰۰ کاراکتر باشد.")
    return cleaned


def validate_bio(bio_val: str | None) -> str | None:
    """Validate bio: optional, max 500 characters."""
    if not bio_val:
        return None
    cleaned = bio_val.strip()
    if not cleaned:
        return None
    if len(cleaned) > 500:
        raise ValueError("متن درباره من نمی‌تواند بیش از ۵۰۰ کاراکتر باشد.")
    return cleaned


def compute_completion_status(display_name: str | None, age: int | None, gender: str | None) -> str:
    """Determine completion status based on required fields."""
    if display_name and age and gender:
        return "COMPLETED"
    return "INCOMPLETE"


class ProfileService:
    """Domain service handling profile queries, creation, and edits."""

    def __init__(self, db_manager: DatabaseManager) -> None:
        self._db_manager = db_manager

    async def get_profile_by_telegram_id(self, telegram_user_id: int) -> UserProfile | None:
        """Fetch a user's profile using their Telegram numeric ID."""
        async with self._db_manager.session() as session:
            stmt = (
                select(UserProfile)
                .join(User, User.id == UserProfile.user_id)
                .where(User.telegram_user_id == telegram_user_id)
                .where(User.status != "DELETED")
            )
            res = await session.execute(stmt)
            return res.scalar_one_or_none()

    async def get_user_with_profile(
        self, telegram_user_id: int
    ) -> tuple[User | None, UserProfile | None]:
        """Fetch both User and UserProfile in one query."""
        async with self._db_manager.session() as session:
            stmt = (
                select(User)
                .options(selectinload(User.profile))
                .where(User.telegram_user_id == telegram_user_id)
                .where(User.status != "DELETED")
            )
            res = await session.execute(stmt)
            user = res.scalar_one_or_none()
            if user is None:
                return None, None
            return user, user.profile

    async def create_profile(
        self,
        telegram_user_id: int,
        data: ProfileData,
    ) -> UserProfile:
        """Create a new profile for a registered adult user.

        Guarantees:
        - Only registered users with adult declaration can create a profile.
        - Idempotent: If a profile already exists, returns the existing profile.
        - Validates all fields before persisting.
        - Sets completion_status to COMPLETED when all required fields are present.
        """
        clean_name = validate_display_name(data.display_name)
        clean_age = validate_age(data.age)
        clean_gender = validate_gender(data.gender)
        clean_city = validate_city(data.city)
        clean_bio = validate_bio(data.bio)
        status = compute_completion_status(clean_name, clean_age, clean_gender)
        visibility = data.visibility if data.visibility in VALID_VISIBILITIES else "PUBLIC"

        async with self._db_manager.session() as session:
            # 1. Fetch user to verify registration and adult declaration
            stmt = (
                select(User)
                .options(selectinload(User.profile))
                .where(User.telegram_user_id == telegram_user_id)
                .where(User.status != "DELETED")
            )
            res = await session.execute(stmt)
            user = res.scalar_one_or_none()

            if user is None or not user.age_18_plus_declared:
                raise PermissionError("کاربر ثبت‌نام‌نشده یا شرایط سنی تأیید نشده است.")
            await reconcile_user_suspension_in_session(session, user)
            if user.status != "ACTIVE":
                raise PermissionError("حساب کاربری شما غیرفعال یا تعلیق شده است.")

            # 2. Check if profile already exists
            if user.profile is not None:
                logger.info(
                    "Profile already exists for user telegram_id=%s (user_id=%s)",
                    telegram_user_id,
                    user.id,
                )
                return user.profile

            # 3. Create profile
            utc_now = now_utc()
            profile = UserProfile(
                user_id=user.id,
                age_18_plus_declared=user.age_18_plus_declared,
                age_declared_date=user.age_declared_date,
                age=clean_age,
                display_name=clean_name,
                gender=clean_gender,
                city=clean_city,
                bio=clean_bio,
                completion_status=status,
                visibility=visibility,
                created_at=utc_now,
                updated_at=utc_now,
            )

            session.add(profile)
            await session.flush()
            await session.refresh(profile)
            logger.info("Created new profile for user_id=%s", user.id)
            return profile

    async def update_profile_field(
        self,
        telegram_user_id: int,
        field_name: Literal["display_name", "age", "gender", "city", "bio"],
        new_value: str | int | None,
    ) -> UserProfile:
        """Update a specific field on the user's profile with validation."""
        async with self._db_manager.session() as session:
            stmt = (
                select(UserProfile, User)
                .join(User, User.id == UserProfile.user_id)
                .where(User.telegram_user_id == telegram_user_id)
                .where(User.status != "DELETED")
            )
            row = (await session.execute(stmt)).first()

            if row is None:
                raise LookupError("پروفایلی برای این کاربر یافت نشد.")
            profile, user = row
            await reconcile_user_suspension_in_session(session, user)
            if user.status != "ACTIVE":
                raise PermissionError("حساب کاربری شما غیرفعال یا تعلیق شده است.")

            if field_name == "display_name":
                profile.display_name = validate_display_name(str(new_value) if new_value else "")
            elif field_name == "age":
                profile.age = validate_age(new_value)
            elif field_name == "gender":
                profile.gender = validate_gender(str(new_value) if new_value else "")
            elif field_name == "city":
                profile.city = validate_city(str(new_value) if new_value is not None else None)
            elif field_name == "bio":
                profile.bio = validate_bio(str(new_value) if new_value is not None else None)
            else:
                raise ValueError(f"فیلد نامعتبر: {field_name}")

            profile.completion_status = compute_completion_status(
                profile.display_name, profile.age, profile.gender
            )
            profile.updated_at = now_utc()

            await session.flush()
            await session.refresh(profile)
            logger.info("Updated profile %s field %s", profile.user_id, field_name)
            return profile
