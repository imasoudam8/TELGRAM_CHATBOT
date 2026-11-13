"""Out-of-band CLI bootstrap utility to provision an initial SUPER_ADMIN safely."""

import argparse
import asyncio
import sys

from sqlalchemy import select

from src.app.config.settings import get_settings
from src.app.infrastructure.database.base import now_utc
from src.app.infrastructure.database.session import DatabaseManager
from src.app.modules.admin.audit import record_audit_log_in_session
from src.app.modules.admin.models import AdminUser
from src.app.modules.admin.rbac import ROLE_SUPER_ADMIN
from src.app.modules.admin.schemas import (
    AdminConflictError,
    AdminNotFoundError,
    AdminRoleDTO,
    AdminValidationError,
)
from src.app.modules.moderation.enforcement import (
    as_utc,
    reconcile_user_suspension_in_session,
)
from src.app.modules.users.models import User


async def bootstrap_super_admin(
    db_manager: DatabaseManager,
    *,
    telegram_user_id: int | None = None,
    public_id: str | None = None,
    reason: str = "Out-of-band SUPER_ADMIN bootstrap",
) -> tuple[AdminRoleDTO, int]:
    """Promote an existing active registered user to active SUPER_ADMIN with audit logging.

    Designed strictly for explicit operator invocation via CLI. Never auto-provisions
    admins inside runtime bot handlers and never hardcodes any Telegram IDs.
    """
    has_tg = telegram_user_id is not None
    has_pid = bool(public_id and public_id.strip())
    if has_tg == has_pid:
        raise AdminValidationError(
            "باید دقیقاً یکی از شناسه‌های telegram_user_id یا public_id مشخص شود."
        )

    clean_reason = reason.strip() if reason else ""
    if not clean_reason:
        raise AdminValidationError("ذکر دلیل برای ثبت دسترسی سوپر ادمین الزامی است.")

    utc_now = now_utc()
    async with db_manager.session() as session:
        if telegram_user_id is not None:
            stmt = select(User).where(
                User.telegram_user_id == telegram_user_id, User.status != "DELETED"
            )
        else:
            stmt = select(User).where(
                User.public_id == public_id.strip(),  # type: ignore[union-attr]
                User.status != "DELETED",
            )

        user = (await session.execute(stmt.with_for_update())).scalar_one_or_none()
        if user is None:
            raise AdminNotFoundError(
                "کاربر مورد نظر یافت نشد. ابتدا کاربر باید در ربات ثبت‌نام (تأیید سن ۱۸+) کرده باشد."
            )

        await reconcile_user_suspension_in_session(session, user, utc_now=utc_now)
        if user.status != "ACTIVE" or not user.age_18_plus_declared:
            raise AdminConflictError(
                f"امکان انتصاب سوپر ادمین برای کاربر با وضعیت {user.status} وجود ندارد."
            )

        existing = (
            await session.execute(
                select(AdminUser).where(AdminUser.user_id == user.id).with_for_update()
            )
        ).scalar_one_or_none()

        payload_before = (
            {"role": existing.role, "is_active": existing.is_active}
            if existing is not None
            else None
        )

        if existing is None:
            admin_row = AdminUser(
                user_id=user.id,
                role=ROLE_SUPER_ADMIN,
                is_active=True,
                assigned_by=None,
                created_at=utc_now,
                updated_at=utc_now,
            )
            session.add(admin_row)
        else:
            existing.role = ROLE_SUPER_ADMIN
            existing.is_active = True
            existing.updated_at = utc_now
            admin_row = existing

        await session.flush()

        audit_entry = await record_audit_log_in_session(
            session,
            admin_user_id=user.id,
            action="ADMIN_ROLE_BOOTSTRAP",
            target_type="ADMIN_USER",
            target_id=str(user.id),
            payload_before=payload_before,
            payload_after={
                "role": ROLE_SUPER_ADMIN,
                "is_active": True,
                "bootstrap": True,
            },
            reason=clean_reason,
            utc_now=utc_now,
        )

        return (
            AdminRoleDTO(
                user_id=user.id,
                public_id=user.public_id,
                role=admin_row.role,
                is_active=admin_row.is_active,
                assigned_by_public_id=None,
                updated_at=as_utc(admin_row.updated_at),
            ),
            audit_entry.id,
        )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Provision or reactivate an out-of-band SUPER_ADMIN user."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--telegram-id",
        type=int,
        help="Telegram numeric user ID of an already registered active user.",
    )
    group.add_argument(
        "--public-id",
        type=str,
        help="Public ID (usr_...) of an already registered active user.",
    )
    parser.add_argument(
        "--reason",
        type=str,
        default="Out-of-band SUPER_ADMIN CLI bootstrap",
        help="Mandatory non-empty audit log reason.",
    )
    return parser


async def _async_main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    settings = get_settings()
    db_manager = DatabaseManager(settings)
    try:
        dto, audit_id = await bootstrap_super_admin(
            db_manager,
            telegram_user_id=args.telegram_id,
            public_id=args.public_id,
            reason=args.reason,
        )
        print(
            f"Bootstrapped SUPER_ADMIN: public_id={dto.public_id} "
            f"role={dto.role} active={dto.is_active} audit_log_id={audit_id}"
        )
        return 0
    except Exception as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await db_manager.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_async_main()))
