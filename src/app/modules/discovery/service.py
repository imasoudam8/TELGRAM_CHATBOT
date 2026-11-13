"""Profile discovery and user preferences domain service."""

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.infrastructure.database.base import now_utc
from src.app.infrastructure.database.session import DatabaseManager
from src.app.modules.chat.models import ActiveChatParticipant, UserBlock
from src.app.modules.discovery.schemas import (
    ALLOWED_ACTIVITY_WINDOWS_HOURS,
    RELAXABLE_FIELD_KEYS,
    DiscoveryPageDTO,
    PublicProfileDTO,
    UserPreferencesDTO,
)
from src.app.modules.moderation.enforcement import reconcile_user_suspension_in_session
from src.app.modules.profiles.service import VALID_GENDERS
from src.app.modules.users.models import User, UserPreferences, UserProfile

logger = logging.getLogger(__name__)


def preferences_model_to_dto(
    user_id: int,
    prefs: UserPreferences | None,
) -> UserPreferencesDTO:
    """Convert ORM UserPreferences (or None) into a normalized UserPreferencesDTO."""
    if prefs is None:
        return UserPreferencesDTO(user_id=user_id)

    req_filters: dict[str, Any] = dict(prefs.required_filters or {})
    rel_filters: dict[str, Any] = dict(prefs.relaxable_filters or {})

    raw_hours = req_filters.get("recent_activity_hours")
    recent_hours = int(raw_hours) if isinstance(raw_hours, int) and raw_hours > 0 else None

    relaxable_fields = {key for key in RELAXABLE_FIELD_KEYS if rel_filters.get(key) is True}

    return UserPreferencesDTO(
        user_id=user_id,
        preferred_genders=list(prefs.preferred_genders or []),
        min_age=prefs.min_age,
        max_age=prefs.max_age,
        preferred_cities=list(prefs.preferred_cities or []),
        recent_activity_hours=recent_hours,
        relaxable_fields=relaxable_fields,
    )


def validate_preferences_inputs(
    preferred_genders: list[str] | None = None,
    min_age: int = 18,
    max_age: int = 99,
    preferred_cities: list[str] | None = None,
    recent_activity_hours: int | None = None,
    relaxable_fields: set[str] | None = None,
) -> tuple[list[str], int, int, list[str], int | None, set[str]]:
    """Validate preference fields against database constraints and product rules."""
    clean_genders: list[str] = []
    if preferred_genders:
        seen_g: set[str] = set()
        for g in preferred_genders:
            norm = g.strip().upper()
            if norm not in VALID_GENDERS:
                raise ValueError("جنسیت انتخاب‌شده در فیلترها معتبر نیست.")
            if norm not in seen_g:
                seen_g.add(norm)
                clean_genders.append(norm)

    if not isinstance(min_age, int) or not isinstance(max_age, int):
        raise ValueError("بازه سنی باید شامل اعداد صحیح باشد.")
    if min_age < 18 or max_age > 99 or min_age > max_age:
        raise ValueError("بازه سنی باید بین ۱۸ تا ۹۹ سال باشد و حداقل سن از حداکثر بیشتر نباشد.")

    clean_cities: list[str] = []
    if preferred_cities:
        seen_c: set[str] = set()
        for c in preferred_cities:
            stripped = c.strip()
            if not stripped:
                continue
            if len(stripped) > 100:
                raise ValueError("نام هر شهر نمی‌تواند بیشتر از ۱۰۰ کاراکتر باشد.")
            lowered = stripped.lower()
            if lowered not in seen_c:
                seen_c.add(lowered)
                clean_cities.append(stripped)
        if len(clean_cities) > 20:
            raise ValueError("حداکثر ۲۰ شهر می‌تواند انتخاب شود.")

    if recent_activity_hours is not None:
        if (
            not isinstance(recent_activity_hours, int)
            or recent_activity_hours not in ALLOWED_ACTIVITY_WINDOWS_HOURS
        ):
            raise ValueError("بازه فعالیت اخیر انتخاب‌شده معتبر نیست.")

    clean_relaxable: set[str] = set()
    if relaxable_fields:
        invalid_keys = relaxable_fields - RELAXABLE_FIELD_KEYS
        if invalid_keys:
            raise ValueError("فیلدهای قابل انعطاف انتخاب‌شده معتبر نیستند.")
        clean_relaxable = set(relaxable_fields)

    return clean_genders, min_age, max_age, clean_cities, recent_activity_hours, clean_relaxable


def profile_matches_preferences(
    prefs: UserPreferencesDTO,
    target_profile: PublicProfileDTO,
    *,
    allow_relaxed: bool = False,
    now_ts: Any | None = None,
) -> bool:
    """Evaluate in-memory whether a candidate profile satisfies preference filters."""
    ref_now = now_ts or now_utc()

    if prefs.preferred_genders and not (allow_relaxed and "gender" in prefs.relaxable_fields):
        if target_profile.gender not in prefs.preferred_genders:
            return False

    if not (allow_relaxed and "age" in prefs.relaxable_fields):
        if target_profile.age < prefs.min_age or target_profile.age > prefs.max_age:
            return False

    if prefs.preferred_cities and not (allow_relaxed and "city" in prefs.relaxable_fields):
        if not target_profile.city:
            return False
        allowed_lower = {c.strip().lower() for c in prefs.preferred_cities}
        if target_profile.city.strip().lower() not in allowed_lower:
            return False

    if prefs.recent_activity_hours is not None and not (
        allow_relaxed and "recent_activity" in prefs.relaxable_fields
    ):
        cutoff = ref_now - timedelta(hours=prefs.recent_activity_hours)
        if target_profile.last_activity_at < cutoff:
            return False

    return True


def _to_public_dto(
    user: User,
    profile: UserProfile,
    *,
    in_active_chat: bool = False,
) -> PublicProfileDTO:
    return PublicProfileDTO(
        user_id=user.id,
        public_id=user.public_id,
        display_name=profile.display_name,
        age=profile.age,
        gender=profile.gender,
        city=profile.city,
        bio=profile.bio,
        last_activity_at=user.last_activity_at,
        in_active_chat=in_active_chat,
    )


class DiscoveryService:
    """Service managing user discovery preferences, filtered browsing, and public profile lookup."""

    def __init__(self, db_manager: DatabaseManager) -> None:
        self._db_manager = db_manager

    async def get_preferences_by_telegram_id(
        self, telegram_user_id: int
    ) -> UserPreferencesDTO | None:
        """Fetch user preferences using their Telegram numeric ID."""
        async with self._db_manager.session() as session:
            user = await self._get_eligible_viewer_by_telegram(session, telegram_user_id)
            if user is None:
                return None
            return await self._load_or_default_preferences(session, user.id)

    async def get_preferences(self, user_id: int) -> UserPreferencesDTO:
        """Fetch user preferences (or default DTO if not yet persisted)."""
        async with self._db_manager.session() as session:
            return await self._load_or_default_preferences(session, user_id)

    async def save_preferences(
        self,
        user_id: int,
        *,
        preferred_genders: list[str] | None = None,
        min_age: int = 18,
        max_age: int = 99,
        preferred_cities: list[str] | None = None,
        recent_activity_hours: int | None = None,
        relaxable_fields: set[str] | None = None,
    ) -> UserPreferencesDTO:
        """Validate and persist UserPreferences for a user."""
        (
            clean_genders,
            clean_min_age,
            clean_max_age,
            clean_cities,
            clean_hours,
            clean_relaxable,
        ) = validate_preferences_inputs(
            preferred_genders=preferred_genders,
            min_age=min_age,
            max_age=max_age,
            preferred_cities=preferred_cities,
            recent_activity_hours=recent_activity_hours,
            relaxable_fields=relaxable_fields,
        )

        utc_now = now_utc()
        required_filters: dict[str, Any] = {
            "gender": "gender" not in clean_relaxable and bool(clean_genders),
            "age": "age" not in clean_relaxable and (clean_min_age > 18 or clean_max_age < 99),
            "city": "city" not in clean_relaxable and bool(clean_cities),
            "recent_activity": (
                "recent_activity" not in clean_relaxable and clean_hours is not None
            ),
        }
        if clean_hours is not None:
            required_filters["recent_activity_hours"] = clean_hours

        relaxable_dict = {key: (key in clean_relaxable) for key in sorted(RELAXABLE_FIELD_KEYS)}

        async with self._db_manager.session() as session:
            user_res = await session.execute(select(User).where(User.id == user_id))
            user = user_res.scalar_one_or_none()
            await reconcile_user_suspension_in_session(session, user, utc_now=utc_now)
            if user is None or user.status != "ACTIVE" or not user.age_18_plus_declared:
                raise PermissionError("کاربر فعال یا واجد شرایط یافت نشد.")

            user.last_activity_at = utc_now

            prefs_res = await session.execute(
                select(UserPreferences).where(UserPreferences.user_id == user_id)
            )
            prefs = prefs_res.scalar_one_or_none()
            if prefs is None:
                prefs = UserPreferences(
                    user_id=user_id,
                    preferred_genders=clean_genders,
                    min_age=clean_min_age,
                    max_age=clean_max_age,
                    preferred_cities=clean_cities,
                    required_filters=required_filters,
                    relaxable_filters=relaxable_dict,
                    created_at=utc_now,
                    updated_at=utc_now,
                )
                session.add(prefs)
            else:
                prefs.preferred_genders = clean_genders
                prefs.min_age = clean_min_age
                prefs.max_age = clean_max_age
                prefs.preferred_cities = clean_cities
                prefs.required_filters = required_filters
                prefs.relaxable_filters = relaxable_dict
                prefs.updated_at = utc_now

            await session.flush()
            return preferences_model_to_dto(user_id, prefs)

    async def browse_profiles(
        self,
        viewer_user_id: int,
        *,
        offset: int = 0,
        limit: int = 1,
        use_relaxed: bool = False,
    ) -> DiscoveryPageDTO:
        """Browse eligible public profiles respecting filters, blocks, and active chats.

        Never silently relaxes required filters. Only evaluates relaxable alternatives
        when explicitly permitted by user's relaxable_filters.
        """
        if offset < 0:
            offset = 0
        if limit < 1:
            limit = 1
        if limit > 20:
            limit = 20

        utc_now = now_utc()

        async with self._db_manager.session() as session:
            viewer_res = await session.execute(select(User).where(User.id == viewer_user_id))
            viewer = viewer_res.scalar_one_or_none()
            await reconcile_user_suspension_in_session(session, viewer, utc_now=utc_now)
            if viewer is None or viewer.status != "ACTIVE" or not viewer.age_18_plus_declared:
                raise PermissionError("دسترسی مجاز نیست.")

            viewer.last_activity_at = utc_now
            prefs = await self._load_or_default_preferences(session, viewer_user_id)

            # Never apply relaxation unless user explicitly enabled relaxable filters
            effective_relaxed = use_relaxed and prefs.has_relaxable_active_filters

            base_filters = self._build_eligibility_conditions(viewer_user_id)
            pref_filters = self._build_preference_conditions(
                prefs, use_relaxed=effective_relaxed, ref_now=utc_now
            )
            all_filters = [*base_filters, *pref_filters]

            count_stmt = (
                select(func.count(User.id))
                .join(UserProfile, UserProfile.user_id == User.id)
                .where(*all_filters)
            )
            total_count = int((await session.execute(count_stmt)).scalar_one())

            items: list[PublicProfileDTO] = []
            if total_count > 0 and offset < total_count:
                query_stmt = (
                    select(User, UserProfile)
                    .join(UserProfile, UserProfile.user_id == User.id)
                    .where(*all_filters)
                    .order_by(User.last_activity_at.desc(), User.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
                rows = (await session.execute(query_stmt)).all()
                items = [_to_public_dto(u, p, in_active_chat=False) for u, p in rows]

            relaxed_available = False
            relaxed_count = 0
            if not effective_relaxed and total_count < limit and prefs.has_relaxable_active_filters:
                relaxed_filters = self._build_preference_conditions(
                    prefs, use_relaxed=True, ref_now=utc_now
                )
                relaxed_count_stmt = (
                    select(func.count(User.id))
                    .join(UserProfile, UserProfile.user_id == User.id)
                    .where(*base_filters, *relaxed_filters)
                )
                relaxed_count = int((await session.execute(relaxed_count_stmt)).scalar_one())
                relaxed_available = relaxed_count > total_count

            return DiscoveryPageDTO(
                items=items,
                total_count=total_count,
                offset=offset,
                limit=limit,
                has_next=(offset + len(items)) < total_count,
                has_prev=offset > 0,
                used_relaxed=effective_relaxed,
                relaxed_available=relaxed_available,
                relaxed_count=relaxed_count,
            )

    async def get_public_profile_by_public_id(
        self,
        viewer_user_id: int,
        public_id: str,
    ) -> PublicProfileDTO | None:
        """Open a user's public profile by internal public_id, enforcing visibility and blocks."""
        clean_public_id = public_id.strip()
        if not clean_public_id:
            return None

        async with self._db_manager.session() as session:
            block_exists = exists().where(
                or_(
                    and_(UserBlock.blocker_id == viewer_user_id, UserBlock.blocked_id == User.id),
                    and_(UserBlock.blocker_id == User.id, UserBlock.blocked_id == viewer_user_id),
                )
            )
            active_exists = exists().where(ActiveChatParticipant.user_id == User.id)

            stmt = (
                select(User, UserProfile, active_exists.label("is_active_chat"))
                .join(UserProfile, UserProfile.user_id == User.id)
                .where(
                    User.public_id == clean_public_id,
                    User.id != viewer_user_id,
                    User.status == "ACTIVE",
                    User.age_18_plus_declared.is_(True),
                    UserProfile.completion_status == "COMPLETED",
                    UserProfile.visibility == "PUBLIC",
                    ~block_exists,
                )
            )
            row = (await session.execute(stmt)).first()
            if row is None:
                return None

            target_user, target_profile, is_active_chat = row
            return _to_public_dto(
                target_user,
                target_profile,
                in_active_chat=bool(is_active_chat),
            )

    @staticmethod
    async def _get_eligible_viewer_by_telegram(
        session: AsyncSession, telegram_user_id: int
    ) -> User | None:
        res = await session.execute(
            select(User).where(
                User.telegram_user_id == telegram_user_id,
                User.status != "DELETED",
                User.age_18_plus_declared.is_(True),
            )
        )
        user = res.scalar_one_or_none()
        if user is None:
            return None
        await reconcile_user_suspension_in_session(session, user)
        if user.status != "ACTIVE":
            return None
        return user

    @staticmethod
    async def _load_or_default_preferences(
        session: AsyncSession, user_id: int
    ) -> UserPreferencesDTO:
        res = await session.execute(
            select(UserPreferences).where(UserPreferences.user_id == user_id)
        )
        return preferences_model_to_dto(user_id, res.scalar_one_or_none())

    @staticmethod
    def _build_eligibility_conditions(viewer_user_id: int) -> list[Any]:
        block_exists = exists().where(
            or_(
                and_(UserBlock.blocker_id == viewer_user_id, UserBlock.blocked_id == User.id),
                and_(UserBlock.blocker_id == User.id, UserBlock.blocked_id == viewer_user_id),
            )
        )
        active_chat_exists = exists().where(ActiveChatParticipant.user_id == User.id)
        return [
            User.id != viewer_user_id,
            User.status == "ACTIVE",
            User.age_18_plus_declared.is_(True),
            UserProfile.completion_status == "COMPLETED",
            UserProfile.visibility == "PUBLIC",
            ~block_exists,
            ~active_chat_exists,
        ]

    @staticmethod
    def _build_preference_conditions(
        prefs: UserPreferencesDTO,
        *,
        use_relaxed: bool,
        ref_now: Any,
    ) -> list[Any]:
        conditions: list[Any] = []

        if prefs.preferred_genders and not (use_relaxed and "gender" in prefs.relaxable_fields):
            conditions.append(UserProfile.gender.in_(prefs.preferred_genders))

        if not (use_relaxed and "age" in prefs.relaxable_fields):
            conditions.append(UserProfile.age >= prefs.min_age)
            conditions.append(UserProfile.age <= prefs.max_age)

        if prefs.preferred_cities and not (use_relaxed and "city" in prefs.relaxable_fields):
            lowered_cities = [c.strip().lower() for c in prefs.preferred_cities if c.strip()]
            if lowered_cities:
                conditions.append(UserProfile.city.is_not(None))
                conditions.append(func.lower(func.trim(UserProfile.city)).in_(lowered_cities))

        if prefs.recent_activity_hours is not None and not (
            use_relaxed and "recent_activity" in prefs.relaxable_fields
        ):
            cutoff = ref_now - timedelta(hours=prefs.recent_activity_hours)
            conditions.append(User.last_activity_at >= cutoff)

        return conditions
