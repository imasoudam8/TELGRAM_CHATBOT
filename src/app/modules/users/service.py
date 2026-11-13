"""User identity and adult self-declaration registration service."""

import logging
import secrets
import string
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.infrastructure.database.base import get_business_date, now_utc
from src.app.infrastructure.database.session import DatabaseManager
from src.app.modules.moderation.enforcement import reconcile_user_suspension_in_session
from src.app.modules.users.models import User

logger = logging.getLogger(__name__)

# Character set for public ID generation: lowercase alphanumeric
_PUBLIC_ID_CHARS = string.ascii_lowercase + string.digits
_PUBLIC_ID_LENGTH = 12


def generate_public_id() -> str:
    """Generate a non-sequential, cryptographically secure random public ID.

    The format is 'usr_' followed by 12 lowercase alphanumeric characters,
    ensuring it is non-sequential and completely independent of the Telegram ID.
    """
    random_suffix = "".join(secrets.choice(_PUBLIC_ID_CHARS) for _ in range(_PUBLIC_ID_LENGTH))
    return f"usr_{random_suffix}"


class UserService:
    """Service managing user lookup, adult onboarding self-declaration, and account status."""

    def __init__(self, db_manager: DatabaseManager) -> None:
        self._db_manager = db_manager

    async def get_user_by_telegram_id(self, telegram_user_id: int) -> User | None:
        """Retrieve an existing user by their Telegram numeric ID."""
        async with self._db_manager.session() as session:
            return await self.get_user_by_session(session, telegram_user_id)

    @staticmethod
    async def get_user_by_session(
        session: AsyncSession,
        telegram_user_id: int,
    ) -> User | None:
        """Query user by Telegram ID using an active transactional session."""
        stmt = (
            select(User)
            .where(User.telegram_user_id == telegram_user_id)
            .where(User.status != "DELETED")
        )
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()
        if user is not None:
            await reconcile_user_suspension_in_session(session, user)
        return user

    async def register_adult_user(
        self,
        telegram_user_id: int,
        business_tz: ZoneInfo,
    ) -> tuple[User, bool]:
        """Register a user upon affirmative adult self-declaration.

        Guarantees:
          - Idempotent: repeated attempts return the existing user record.
          - Concurrency-safe: database constraints resolve race conditions safely.
          - Timezone-aware: records declaration timestamp in UTC and declaration date
            in configured business timezone.

        Returns:
            tuple[User, bool]: (user_instance, is_newly_created)
        """
        async with self._db_manager.session() as session:
            # 1. Fast check for existing user
            existing = await self.get_user_by_session(session, telegram_user_id)
            if existing is not None:
                logger.info(
                    "User with telegram_id=%s already exists (public_id=%s)",
                    telegram_user_id,
                    existing.public_id,
                )
                return existing, False

            # 2. Attempt registration with collision handling on public_id
            max_attempts = 5
            for attempt in range(max_attempts):
                public_id = generate_public_id()
                utc_now = now_utc()
                declared_date = get_business_date(utc_now, business_tz)

                new_user = User(
                    telegram_user_id=telegram_user_id,
                    public_id=public_id,
                    status="ACTIVE",
                    age_18_plus_declared=True,
                    age_declared_at=utc_now,
                    age_declared_date=declared_date,
                    created_at=utc_now,
                    last_activity_at=utc_now,
                )

                try:
                    async with session.begin_nested():
                        session.add(new_user)
                        await session.flush()

                    await session.refresh(new_user)
                    logger.info(
                        "Registered new adult user (public_id=%s) for telegram_id=%s",
                        public_id,
                        telegram_user_id,
                    )
                    return new_user, True

                except IntegrityError as exc:
                    err_text = str(exc).lower()
                    if "telegram_user_id" in err_text or "uq_users_telegram_user_id" in err_text:
                        logger.warning(
                            "Concurrent registration race detected for telegram_id=%s; "
                            "fetching existing user.",
                            telegram_user_id,
                        )
                        existing_after_race = await self.get_user_by_session(
                            session, telegram_user_id
                        )
                        if existing_after_race is not None:
                            return existing_after_race, False
                        raise

                    if "public_id" in err_text or "uq_users_public_id" in err_text:
                        logger.warning(
                            "Public ID collision '%s' on attempt %d; retrying.",
                            public_id,
                            attempt + 1,
                        )
                        continue

                    # Other unexpected integrity error
                    logger.error("Unexpected integrity error registering user: %s", exc)
                    raise

            raise RuntimeError(
                f"Failed to generate unique public_id after {max_attempts} attempts."
            )
