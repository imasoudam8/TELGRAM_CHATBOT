"""Role-based access control (RBAC) enforcement backed by AdminUser persistence."""

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.infrastructure.database.base import now_utc
from src.app.modules.admin.models import AdminUser
from src.app.modules.admin.schemas import AdminActorDTO, AdminPermissionError
from src.app.modules.moderation.enforcement import reconcile_user_suspension_in_session
from src.app.modules.users.models import User

ROLE_SUPER_ADMIN = "SUPER_ADMIN"
ROLE_MODERATOR = "MODERATOR"
ROLE_SUPPORT = "SUPPORT"

VALID_ADMIN_ROLES = frozenset({ROLE_SUPER_ADMIN, ROLE_MODERATOR, ROLE_SUPPORT})

ADMIN_PANEL_ROLES = frozenset({ROLE_SUPER_ADMIN, ROLE_MODERATOR, ROLE_SUPPORT})
USER_INSPECT_ROLES = frozenset({ROLE_SUPER_ADMIN, ROLE_MODERATOR, ROLE_SUPPORT})
REPORT_VIEW_ROLES = frozenset({ROLE_SUPER_ADMIN, ROLE_MODERATOR, ROLE_SUPPORT})
WARNING_ROLES = frozenset({ROLE_SUPER_ADMIN, ROLE_MODERATOR, ROLE_SUPPORT})

MODERATOR_CONTROL_ROLES = frozenset({ROLE_SUPER_ADMIN, ROLE_MODERATOR})
SUPER_ADMIN_ONLY_ROLES = frozenset({ROLE_SUPER_ADMIN})

ROLE_LABELS_PERSIAN: dict[str, str] = {
    ROLE_SUPER_ADMIN: "سوپر ادمین (دسترسی کامل)",
    ROLE_MODERATOR: "ناظر (Moderation)",
    ROLE_SUPPORT: "پشتیبان (Support)",
}


async def resolve_active_admin_in_session(
    session: AsyncSession,
    *,
    telegram_user_id: int | None = None,
    user_id: int | None = None,
) -> AdminActorDTO | None:
    """Resolve an active, non-restricted AdminUser record from DB without trusting UI state."""
    stmt = select(User, AdminUser).join(AdminUser, AdminUser.user_id == User.id)
    if telegram_user_id is not None:
        stmt = stmt.where(User.telegram_user_id == telegram_user_id)
    elif user_id is not None:
        stmt = stmt.where(User.id == user_id)
    else:
        return None

    row = (await session.execute(stmt)).first()
    if row is None:
        return None

    user, admin_row = row
    utc_now = now_utc()
    await reconcile_user_suspension_in_session(session, user, utc_now=utc_now)

    if user.status != "ACTIVE" or not admin_row.is_active:
        return None
    if admin_row.role not in VALID_ADMIN_ROLES:
        return None

    return AdminActorDTO(
        user_id=user.id,
        public_id=user.public_id,
        role=admin_row.role,
        is_active=True,
    )


async def require_admin_actor_in_session(
    session: AsyncSession,
    *,
    allowed_roles: Iterable[str],
    actor_telegram_id: int | None = None,
    actor_user_id: int | None = None,
) -> AdminActorDTO:
    """Verify active admin membership and required role in DB or raise AdminPermissionError."""
    actor = await resolve_active_admin_in_session(
        session,
        telegram_user_id=actor_telegram_id,
        user_id=actor_user_id,
    )
    if actor is None:
        raise AdminPermissionError("⛔ شما دسترسی مدیریتی فعال به این بخش ندارید.")

    allowed_set = set(allowed_roles)
    if actor.role not in allowed_set:
        raise AdminPermissionError("⛔ سطح دسترسی نقش شما برای انجام این عملیات کافی نیست.")

    return actor
