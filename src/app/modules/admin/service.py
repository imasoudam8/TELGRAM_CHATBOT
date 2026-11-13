"""Core administrative, moderation queue, wallet control, and role management service."""

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, desc, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.config.settings import Settings, get_settings
from src.app.infrastructure.database.base import get_business_date, now_utc
from src.app.infrastructure.database.session import DatabaseManager
from src.app.modules.admin.audit import record_audit_log_in_session
from src.app.modules.admin.models import AdminAuditLog, AdminUser
from src.app.modules.admin.rbac import (
    MODERATOR_CONTROL_ROLES,
    REPORT_VIEW_ROLES,
    ROLE_SUPER_ADMIN,
    SUPER_ADMIN_ONLY_ROLES,
    USER_INSPECT_ROLES,
    VALID_ADMIN_ROLES,
    WARNING_ROLES,
    require_admin_actor_in_session,
    resolve_active_admin_in_session,
)
from src.app.modules.admin.schemas import (
    AdminActorDTO,
    AdminAuditLogDTO,
    AdminAuditLogPageDTO,
    AdminBlockOutcomeDTO,
    AdminConflictError,
    AdminModerationSummaryDTO,
    AdminNotFoundError,
    AdminPermissionError,
    AdminRoleDTO,
    AdminUserDetailDTO,
    AdminValidationError,
    AdminWalletOpDTO,
    EvidenceHoldMetadataDTO,
    ModerationOutcomeDTO,
    ReportDetailDTO,
    ReportQueueItemDTO,
    ReportQueuePageDTO,
)
from src.app.modules.chat.models import (
    ActiveChatParticipant,
    ChatMessage,
    ChatSession,
    DirectRequest,
    MessageEvidenceHold,
    UserBlock,
)
from src.app.modules.moderation.enforcement import (
    as_utc,
    evaluate_user_restrictions_in_session,
    reconcile_user_suspension_in_session,
)
from src.app.modules.moderation.models import ModerationAction, Report
from src.app.modules.moderation.schemas import MODERATION_ACTION_TYPES, REPORT_STATUSES
from src.app.modules.users.models import User, UserProfile
from src.app.modules.wallet.models import WalletLedger
from src.app.modules.wallet.schemas import TransactionHistoryPageDTO, WalletBalanceDTO
from src.app.modules.wallet.service import WalletService
from src.app.modules.wallet.settlement import ChatSettlementService

logger = logging.getLogger(__name__)

MAX_MODERATION_HOURS = 365 * 24


class AdminService:
    """Authoritative backend service for Telegram administration and moderation."""

    def __init__(self, db_manager: DatabaseManager, settings: Settings | None = None) -> None:
        self._db_manager = db_manager
        self._settings = settings or get_settings()
        self._wallet_service = WalletService(db_manager, self._settings)

    async def get_active_admin_context(self, actor_telegram_id: int) -> AdminActorDTO | None:
        """Return active admin actor context for UI routing or None if unauthorized."""
        async with self._db_manager.session() as session:
            return await resolve_active_admin_in_session(
                session, telegram_user_id=actor_telegram_id
            )

    # -------------------------------------------------------------------------
    # User Lookup & Inspection
    # -------------------------------------------------------------------------
    async def lookup_user_by_public_id(
        self,
        actor_telegram_id: int,
        target_public_id: str,
    ) -> AdminUserDetailDTO:
        """Look up a non-deleted user by public_id and return sanitized operational details."""
        clean_pid = target_public_id.strip()
        if not clean_pid:
            raise AdminValidationError("شناسه عمومی کاربر وارد نشده است.")

        utc_now = now_utc()
        async with self._db_manager.session() as session:
            await require_admin_actor_in_session(
                session,
                allowed_roles=USER_INSPECT_ROLES,
                actor_telegram_id=actor_telegram_id,
            )

            target = (
                await session.execute(
                    select(User).where(User.public_id == clean_pid, User.status != "DELETED")
                )
            ).scalar_one_or_none()
            if target is None:
                raise AdminNotFoundError("کاربری با این شناسه عمومی یافت نشد.")

            restriction = await evaluate_user_restrictions_in_session(
                session, target, utc_now=utc_now
            )

            profile = (
                await session.execute(select(UserProfile).where(UserProfile.user_id == target.id))
            ).scalar_one_or_none()

            admin_row = (
                await session.execute(select(AdminUser).where(AdminUser.user_id == target.id))
            ).scalar_one_or_none()

            in_active_chat = bool(
                (
                    await session.execute(
                        select(exists().where(ActiveChatParticipant.user_id == target.id))
                    )
                ).scalar_one()
            )
            reports_against = int(
                (
                    await session.execute(
                        select(func.count(Report.id)).where(Report.reported_user_id == target.id)
                    )
                ).scalar_one()
            )
            reports_submitted = int(
                (
                    await session.execute(
                        select(func.count(Report.id)).where(Report.reporter_id == target.id)
                    )
                ).scalar_one()
            )
            recent_actions_rows = (
                (
                    await session.execute(
                        select(ModerationAction)
                        .where(ModerationAction.user_id == target.id)
                        .order_by(desc(ModerationAction.created_at), desc(ModerationAction.id))
                        .limit(5)
                    )
                )
                .scalars()
                .all()
            )

            return AdminUserDetailDTO(
                user_id=target.id,
                public_id=target.public_id,
                account_status=target.status,
                age_18_plus_declared=target.age_18_plus_declared,
                created_at=as_utc(target.created_at),
                last_activity_at=as_utc(target.last_activity_at),
                suspended_until=as_utc(target.suspended_until) if target.suspended_until else None,
                suspension_reason=target.suspension_reason,
                is_chat_restricted=restriction.is_chat_restricted,
                chat_restriction_reason=(
                    restriction.restriction_reason if target.status == "ACTIVE" else None
                ),
                chat_restriction_expires_at=(
                    restriction.expires_at if target.status == "ACTIVE" else None
                ),
                admin_role=admin_row.role if admin_row is not None else None,
                admin_is_active=admin_row.is_active if admin_row is not None else None,
                display_name=profile.display_name if profile is not None else None,
                age=profile.age if profile is not None else None,
                gender=profile.gender if profile is not None else None,
                city=profile.city if profile is not None else None,
                profile_completion_status=(
                    profile.completion_status if profile is not None else None
                ),
                profile_visibility=profile.visibility if profile is not None else None,
                in_active_chat=in_active_chat,
                reports_against_count=reports_against,
                reports_submitted_count=reports_submitted,
                recent_actions=tuple(
                    AdminModerationSummaryDTO(
                        id=act.id,
                        action_type=act.action_type,
                        reason=act.reason,
                        expires_at=as_utc(act.expires_at) if act.expires_at else None,
                        created_at=as_utc(act.created_at),
                        report_id=act.report_id,
                    )
                    for act in recent_actions_rows
                ),
            )

    # -------------------------------------------------------------------------
    # Moderation Queue & Report Resolution
    # -------------------------------------------------------------------------
    async def list_reports(
        self,
        actor_telegram_id: int,
        *,
        status_filter: str = "PENDING",
        offset: int = 0,
        limit: int = 5,
    ) -> ReportQueuePageDTO:
        """Return paginated reports queue for authorized staff."""
        norm_status = status_filter.strip().upper()
        if norm_status != "ALL" and norm_status not in REPORT_STATUSES:
            raise AdminValidationError("فیلتر وضعیت گزارش نامعتبر است.")

        safe_offset = max(0, offset)
        safe_limit = max(1, min(limit, 20))

        async with self._db_manager.session() as session:
            await require_admin_actor_in_session(
                session,
                allowed_roles=REPORT_VIEW_ROLES,
                actor_telegram_id=actor_telegram_id,
            )

            count_stmt = select(func.count(Report.id))
            list_stmt = select(Report)
            if norm_status != "ALL":
                count_stmt = count_stmt.where(Report.status == norm_status)
                list_stmt = list_stmt.where(Report.status == norm_status)

            total_count = int((await session.execute(count_stmt)).scalar_one())
            reports = (
                (
                    await session.execute(
                        list_stmt.order_by(Report.created_at.asc(), Report.id.asc())
                        .offset(safe_offset)
                        .limit(safe_limit)
                    )
                )
                .scalars()
                .all()
            )

            user_ids = {r.reporter_id for r in reports} | {r.reported_user_id for r in reports}
            pid_map: dict[int, str] = {}
            if user_ids:
                pid_rows = (
                    await session.execute(
                        select(User.id, User.public_id).where(User.id.in_(list(user_ids)))
                    )
                ).all()
                pid_map = dict(pid_rows)

            items = tuple(
                ReportQueueItemDTO(
                    id=r.id,
                    category=r.category,
                    status=r.status,
                    reporter_public_id=pid_map.get(r.reporter_id, "usr_unknown"),
                    reported_public_id=pid_map.get(r.reported_user_id, "usr_unknown"),
                    chat_session_id=r.chat_session_id,
                    created_at=as_utc(r.created_at),
                )
                for r in reports
            )

            return ReportQueuePageDTO(
                items=items,
                total_count=total_count,
                offset=safe_offset,
                limit=safe_limit,
                has_prev=safe_offset > 0,
                has_next=(safe_offset + len(items)) < total_count,
                status_filter=norm_status,
            )

    async def get_report_detail(
        self,
        actor_telegram_id: int,
        report_id: int,
    ) -> ReportDetailDTO:
        """Inspect full report details and permitted metadata-only evidence holds."""
        utc_now = now_utc()
        async with self._db_manager.session() as session:
            await require_admin_actor_in_session(
                session,
                allowed_roles=REPORT_VIEW_ROLES,
                actor_telegram_id=actor_telegram_id,
            )
            return await self._build_report_detail_in_session(session, report_id, utc_now=utc_now)

    async def resolve_report(
        self,
        actor_telegram_id: int,
        report_id: int,
        *,
        new_status: str,
        resolution_notes: str,
    ) -> tuple[ReportDetailDTO, int]:
        """Resolve a PENDING report safely using FOR UPDATE locking and audit logging."""
        norm_status = new_status.strip().upper()
        if norm_status not in {"REVIEWED", "ACTIONED", "DISMISSED"}:
            raise AdminValidationError(
                "وضعیت نهایی گزارش باید REVIEWED، ACTIONED یا DISMISSED باشد."
            )
        clean_notes = resolution_notes.strip() if resolution_notes else ""
        if not clean_notes:
            raise AdminValidationError("ثبت یادداشت یا دلیل برای تعیین تکلیف گزارش الزامی است.")

        utc_now = now_utc()
        async with self._db_manager.session() as session:
            actor = await require_admin_actor_in_session(
                session,
                allowed_roles=MODERATOR_CONTROL_ROLES,
                actor_telegram_id=actor_telegram_id,
            )

            report = (
                await session.execute(
                    select(Report).where(Report.id == report_id).with_for_update()
                )
            ).scalar_one_or_none()
            if report is None:
                raise AdminNotFoundError("گزارش مورد نظر یافت نشد.")
            if report.status != "PENDING":
                raise AdminConflictError("این گزارش قبلاً توسط یکی از ناظران بررسی و بسته شده است.")

            payload_before = {
                "status": report.status,
                "moderator_id": report.moderator_id,
                "resolution_notes": report.resolution_notes,
            }
            report.status = norm_status
            report.moderator_id = actor.user_id
            report.reviewed_at = utc_now
            report.resolution_notes = clean_notes
            await session.flush()

            audit_entry = await record_audit_log_in_session(
                session,
                admin_user_id=actor.user_id,
                action=f"REPORT_{norm_status}",
                target_type="REPORT",
                target_id=str(report.id),
                payload_before=payload_before,
                payload_after={
                    "status": report.status,
                    "moderator_id": actor.user_id,
                    "resolution_notes": clean_notes,
                },
                reason=clean_notes,
                utc_now=utc_now,
            )
            detail = await self._build_report_detail_in_session(session, report.id, utc_now=utc_now)
            return detail, audit_entry.id

    async def _build_report_detail_in_session(
        self,
        session: AsyncSession,
        report_id: int,
        *,
        utc_now: datetime,
    ) -> ReportDetailDTO:
        report = (
            await session.execute(select(Report).where(Report.id == report_id))
        ).scalar_one_or_none()
        if report is None:
            raise AdminNotFoundError("گزارش مورد نظر یافت نشد.")

        user_ids = [report.reporter_id, report.reported_user_id]
        if report.moderator_id is not None:
            user_ids.append(report.moderator_id)
        users_rows = (
            (await session.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
        )
        users_map = {u.id: u for u in users_rows}
        reporter = users_map.get(report.reporter_id)
        reported = users_map.get(report.reported_user_id)
        moderator = users_map.get(report.moderator_id) if report.moderator_id else None

        if reported is not None:
            reported_restr = await evaluate_user_restrictions_in_session(
                session, reported, utc_now=utc_now
            )
            reported_status = reported.status
            reported_chat_restricted = reported_restr.is_chat_restricted
        else:
            reported_status = "UNKNOWN"
            reported_chat_restricted = False

        holds_stmt = (
            select(MessageEvidenceHold, ChatMessage.message_type)
            .outerjoin(ChatMessage, ChatMessage.id == MessageEvidenceHold.message_id)
            .where(MessageEvidenceHold.report_id == report.id)
            .order_by(MessageEvidenceHold.id.asc())
        )
        holds_rows = (await session.execute(holds_stmt)).all()
        evidence_holds = tuple(
            EvidenceHoldMetadataDTO(
                id=hold.id,
                message_id=hold.message_id,
                chat_session_id=hold.chat_session_id,
                message_content_hash=hold.message_content_hash,
                message_type=msg_type,
                hold_type=hold.hold_type,
                status=hold.status,
                reason=hold.reason,
                created_at=as_utc(hold.created_at),
            )
            for hold, msg_type in holds_rows
        )

        return ReportDetailDTO(
            id=report.id,
            category=report.category,
            status=report.status,
            description=report.description,
            reporter_public_id=reporter.public_id if reporter else "usr_unknown",
            reported_public_id=reported.public_id if reported else "usr_unknown",
            reported_account_status=reported_status,
            reported_is_chat_restricted=reported_chat_restricted,
            chat_session_id=report.chat_session_id,
            reported_message_id=report.reported_message_id,
            moderator_public_id=moderator.public_id if moderator else None,
            resolution_notes=report.resolution_notes,
            created_at=as_utc(report.created_at),
            reviewed_at=as_utc(report.reviewed_at) if report.reviewed_at else None,
            evidence_holds=evidence_holds,
        )

    # -------------------------------------------------------------------------
    # User Moderation Actions & Account Restoration
    # -------------------------------------------------------------------------
    async def apply_moderation_action(
        self,
        actor_telegram_id: int,
        *,
        target_public_id: str,
        action_type: str,
        reason: str,
        duration_hours: int | None = None,
        report_id: int | None = None,
        resolution_notes: str | None = None,
    ) -> ModerationOutcomeDTO:
        """Issue WARNING, TEMPORARY_SUSPENSION, CHAT_RESTRICTION, or PERMANENT_BAN."""
        norm_action = action_type.strip().upper()
        if norm_action not in MODERATION_ACTION_TYPES:
            raise AdminValidationError("نوع اقدام انضباطی نامعتبر است.")

        clean_reason = reason.strip() if reason else ""
        if not clean_reason:
            raise AdminValidationError("ذکر دلیل برای اجرای اقدام انضباطی الزامی است.")

        clean_pid = target_public_id.strip()
        if not clean_pid:
            raise AdminValidationError("شناسه عمومی کاربر هدف وارد نشده است.")

        if norm_action == "WARNING":
            allowed_roles = WARNING_ROLES
        elif norm_action == "PERMANENT_BAN":
            allowed_roles = SUPER_ADMIN_ONLY_ROLES
        else:
            allowed_roles = MODERATOR_CONTROL_ROLES

        if norm_action == "TEMPORARY_SUSPENSION":
            if (
                duration_hours is None
                or isinstance(duration_hours, bool)
                or not isinstance(duration_hours, int)
                or duration_hours < 1
                or duration_hours > MAX_MODERATION_HOURS
            ):
                raise AdminValidationError(
                    f"مدت تعلیق موقت باید عدد صحیح بین ۱ تا {MAX_MODERATION_HOURS} ساعت باشد."
                )
        elif norm_action == "CHAT_RESTRICTION" and duration_hours is not None:
            if (
                isinstance(duration_hours, bool)
                or not isinstance(duration_hours, int)
                or duration_hours < 1
                or duration_hours > MAX_MODERATION_HOURS
            ):
                raise AdminValidationError(
                    f"مدت محدودیت گفتگو باید بین ۱ تا {MAX_MODERATION_HOURS} ساعت باشد."
                )

        utc_now = now_utc()
        expires_at: datetime | None = None
        if norm_action in {"TEMPORARY_SUSPENSION", "CHAT_RESTRICTION"} and duration_hours:
            expires_at = utc_now + timedelta(hours=duration_hours)

        async with self._db_manager.session() as session:
            actor = await require_admin_actor_in_session(
                session,
                allowed_roles=allowed_roles,
                actor_telegram_id=actor_telegram_id,
            )

            target = (
                await session.execute(
                    select(User)
                    .where(User.public_id == clean_pid, User.status != "DELETED")
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if target is None:
                raise AdminNotFoundError("کاربر مورد نظر یافت نشد.")

            if target.id == actor.user_id and norm_action != "WARNING":
                raise AdminConflictError(
                    "امکان اعمال محدودیت یا مسدودسازی روی حساب خودتان وجود ندارد."
                )

            target_admin = (
                await session.execute(
                    select(AdminUser).where(AdminUser.user_id == target.id, AdminUser.is_active)
                )
            ).scalar_one_or_none()
            if (
                target_admin is not None
                and actor.role != ROLE_SUPER_ADMIN
                and norm_action != "WARNING"
            ):
                raise AdminPermissionError(
                    "فقط سوپر ادمین می‌تواند علیه سایر مدیران اقدام انضباطی ثبت کند."
                )

            await reconcile_user_suspension_in_session(session, target, utc_now=utc_now)
            restriction_before = await evaluate_user_restrictions_in_session(
                session, target, utc_now=utc_now
            )
            was_chat_restricted = restriction_before.is_chat_restricted
            payload_before = {
                "status": target.status,
                "suspended_until": target.suspended_until,
                "suspension_reason": target.suspension_reason,
                "was_chat_restricted": was_chat_restricted,
            }

            if report_id is not None:
                report = (
                    await session.execute(
                        select(Report).where(Report.id == report_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if report is None:
                    raise AdminNotFoundError("گزارش مرتبط یافت نشد.")
                if report.reported_user_id != target.id:
                    raise AdminValidationError("گزارش انتخاب‌شده مربوط به این کاربر نیست.")
                if report.status == "PENDING":
                    report.status = "ACTIONED"
                    report.moderator_id = actor.user_id
                    report.reviewed_at = utc_now
                    report.resolution_notes = (
                        resolution_notes.strip()
                        if resolution_notes and resolution_notes.strip()
                        else clean_reason
                    )

            mod_action = ModerationAction(
                user_id=target.id,
                action_type=norm_action,
                moderator_id=actor.user_id,
                report_id=report_id,
                reason=clean_reason,
                expires_at=expires_at,
                created_at=utc_now,
            )
            session.add(mod_action)
            await session.flush()
            await session.refresh(mod_action)

            terminated_chat_id: uuid.UUID | None = None
            terminated_partner_tg_id: int | None = None
            requires_queue_removal = False

            if norm_action == "TEMPORARY_SUSPENSION":
                if target.status == "BANNED":
                    raise AdminConflictError("کاربر به صورت دائم مسدود است.")
                target.status = "SUSPENDED"
                target.suspended_until = expires_at
                target.suspension_reason = clean_reason
                requires_queue_removal = True
                (
                    terminated_chat_id,
                    terminated_partner_tg_id,
                ) = await self._terminate_user_active_chat_in_session(
                    session, target.id, reason="REPORTED", utc_now=utc_now
                )
            elif norm_action == "PERMANENT_BAN":
                target.status = "BANNED"
                target.suspended_until = None
                target.suspension_reason = clean_reason
                requires_queue_removal = True
                if target_admin is not None:
                    target_admin.is_active = False
                    target_admin.updated_at = utc_now
                (
                    terminated_chat_id,
                    terminated_partner_tg_id,
                ) = await self._terminate_user_active_chat_in_session(
                    session, target.id, reason="REPORTED", utc_now=utc_now
                )
            elif norm_action == "CHAT_RESTRICTION":
                requires_queue_removal = True
                (
                    terminated_chat_id,
                    terminated_partner_tg_id,
                ) = await self._terminate_user_active_chat_in_session(
                    session, target.id, reason="REPORTED", utc_now=utc_now
                )

            await session.flush()
            restriction_after = await evaluate_user_restrictions_in_session(
                session, target, utc_now=utc_now
            )

            payload_after = {
                "action_id": mod_action.id,
                "action_type": norm_action,
                "status": target.status,
                "suspended_until": target.suspended_until,
                "suspension_reason": target.suspension_reason,
                "is_chat_restricted": restriction_after.is_chat_restricted,
                "expires_at": expires_at,
                "report_id": report_id,
                "terminated_chat_id": str(terminated_chat_id) if terminated_chat_id else None,
            }

            audit_entry = await record_audit_log_in_session(
                session,
                admin_user_id=actor.user_id,
                action=f"MODERATION_{norm_action}",
                target_type="USER",
                target_id=str(target.id),
                payload_before=payload_before,
                payload_after=payload_after,
                reason=clean_reason,
                utc_now=utc_now,
            )

            return ModerationOutcomeDTO(
                action_id=mod_action.id,
                action_type=norm_action,
                target_user_id=target.id,
                target_public_id=target.public_id,
                target_status=target.status,
                is_chat_restricted=restriction_after.is_chat_restricted,
                expires_at=expires_at,
                audit_log_id=audit_entry.id,
                terminated_chat_id=terminated_chat_id,
                target_telegram_id=target.telegram_user_id,
                terminated_partner_telegram_id=terminated_partner_tg_id,
                requires_queue_removal=requires_queue_removal,
            )

    async def restore_user_access(
        self,
        actor_telegram_id: int,
        *,
        target_public_id: str,
        reason: str,
    ) -> ModerationOutcomeDTO:
        """Restore user from suspension, chat restriction, or (SUPER_ADMIN only) permanent ban."""
        clean_reason = reason.strip() if reason else ""
        if not clean_reason:
            raise AdminValidationError("ذکر دلیل برای رفع محدودیت کاربر الزامی است.")
        clean_pid = target_public_id.strip()
        if not clean_pid:
            raise AdminValidationError("شناسه عمومی کاربر وارد نشده است.")

        utc_now = now_utc()
        async with self._db_manager.session() as session:
            actor = await require_admin_actor_in_session(
                session,
                allowed_roles=MODERATOR_CONTROL_ROLES,
                actor_telegram_id=actor_telegram_id,
            )

            target = (
                await session.execute(
                    select(User)
                    .where(User.public_id == clean_pid, User.status != "DELETED")
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if target is None:
                raise AdminNotFoundError("کاربر مورد نظر یافت نشد.")

            await reconcile_user_suspension_in_session(session, target, utc_now=utc_now)
            restriction_before = await evaluate_user_restrictions_in_session(
                session, target, utc_now=utc_now
            )

            if target.status == "BANNED" and actor.role != ROLE_SUPER_ADMIN:
                raise AdminPermissionError("رفع مسدودیت دائم فقط توسط سوپر ادمین امکان‌پذیر است.")

            if target.status == "ACTIVE" and not restriction_before.is_chat_restricted:
                raise AdminConflictError("این کاربر در حال حاضر هیچ محدودیت یا تعلیق فعالی ندارد.")

            payload_before = {
                "status": target.status,
                "suspended_until": target.suspended_until,
                "suspension_reason": target.suspension_reason,
                "is_chat_restricted": restriction_before.is_chat_restricted,
            }

            target.status = "ACTIVE"
            target.suspended_until = None
            target.suspension_reason = None

            # Expire active CHAT_RESTRICTION / TEMPORARY_SUSPENSION rows so enforcement lifts
            await session.execute(
                update(ModerationAction)
                .where(
                    ModerationAction.user_id == target.id,
                    ModerationAction.action_type.in_(["CHAT_RESTRICTION", "TEMPORARY_SUSPENSION"]),
                    or_(
                        ModerationAction.expires_at.is_(None),
                        ModerationAction.expires_at > utc_now,
                    ),
                )
                .values(expires_at=utc_now)
            )
            await session.flush()

            audit_entry = await record_audit_log_in_session(
                session,
                admin_user_id=actor.user_id,
                action="USER_RESTORE",
                target_type="USER",
                target_id=str(target.id),
                payload_before=payload_before,
                payload_after={
                    "status": "ACTIVE",
                    "suspended_until": None,
                    "is_chat_restricted": False,
                },
                reason=clean_reason,
                utc_now=utc_now,
            )

            return ModerationOutcomeDTO(
                action_id=None,
                action_type="RESTORE",
                target_user_id=target.id,
                target_public_id=target.public_id,
                target_status="ACTIVE",
                is_chat_restricted=False,
                expires_at=None,
                audit_log_id=audit_entry.id,
                target_telegram_id=target.telegram_user_id,
            )

    async def _terminate_user_active_chat_in_session(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        reason: str,
        utc_now: datetime,
    ) -> tuple[uuid.UUID | None, int | None]:
        """Terminate user_id's active chat session atomically and settle if eligible."""
        part = (
            await session.execute(
                select(ActiveChatParticipant).where(ActiveChatParticipant.user_id == user_id)
            )
        ).scalar_one_or_none()
        if part is None:
            return None, None

        chat = (
            await session.execute(
                select(ChatSession).where(ChatSession.id == part.chat_id).with_for_update()
            )
        ).scalar_one_or_none()
        if chat is None:
            await session.execute(
                delete(ActiveChatParticipant).where(ActiveChatParticipant.chat_id == part.chat_id)
            )
            return None, None

        await session.execute(
            delete(ActiveChatParticipant).where(ActiveChatParticipant.chat_id == chat.id)
        )
        if chat.status == "ENDED":
            return None, None

        chat.status = "ENDED"
        chat.end_reason = reason[:50]
        chat.ended_at = utc_now

        partner_id = (
            chat.participant_two_id
            if chat.participant_one_id == user_id
            else chat.participant_one_id
        )
        partner_tg_id = (
            await session.execute(select(User.telegram_user_id).where(User.id == partner_id))
        ).scalar_one_or_none()

        if chat.participant_one_sent_msg and chat.participant_two_sent_msg and not chat.is_charged:
            await ChatSettlementService(self._db_manager, self._settings).settle_chat_in_session(
                session, chat, utc_now=utc_now
            )

        await session.flush()
        return chat.id, partner_tg_id

    # -------------------------------------------------------------------------
    # Moderation Block / Unblock Integration
    # -------------------------------------------------------------------------
    async def admin_block_users(
        self,
        actor_telegram_id: int,
        *,
        blocker_public_id: str,
        blocked_public_id: str,
        reason: str,
        report_id: int | None = None,
    ) -> AdminBlockOutcomeDTO:
        """Create a moderation-authorized block, cancel requests, and end active chat."""
        clean_reason = reason.strip() if reason else ""
        if not clean_reason:
            raise AdminValidationError("ذکر دلیل برای مسدودسازی ارتباط دو کاربر الزامی است.")

        p1 = blocker_public_id.strip()
        p2 = blocked_public_id.strip()
        if not p1 or not p2 or p1 == p2:
            raise AdminValidationError("دو شناسه عمومی معتبر و متفاوت برای مسدودسازی وارد کنید.")

        utc_now = now_utc()
        async with self._db_manager.session() as session:
            actor = await require_admin_actor_in_session(
                session,
                allowed_roles=MODERATOR_CONTROL_ROLES,
                actor_telegram_id=actor_telegram_id,
            )

            users_rows = (
                (
                    await session.execute(
                        select(User).where(User.public_id.in_([p1, p2]), User.status != "DELETED")
                    )
                )
                .scalars()
                .all()
            )
            by_pid = {u.public_id: u for u in users_rows}
            blocker = by_pid.get(p1)
            blocked = by_pid.get(p2)
            if blocker is None or blocked is None:
                raise AdminNotFoundError("یک یا هر دو کاربر مورد نظر یافت نشدند.")

            existing = (
                await session.execute(
                    select(UserBlock).where(
                        UserBlock.blocker_id == blocker.id,
                        UserBlock.blocked_id == blocked.id,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    UserBlock(
                        blocker_id=blocker.id,
                        blocked_id=blocked.id,
                        reason=f"MOD:{clean_reason}"[:100],
                        created_at=utc_now,
                    )
                )
                await session.flush()

            await session.execute(
                update(DirectRequest)
                .where(
                    DirectRequest.status == "PENDING",
                    or_(
                        and_(
                            DirectRequest.requester_id == blocker.id,
                            DirectRequest.target_id == blocked.id,
                        ),
                        and_(
                            DirectRequest.requester_id == blocked.id,
                            DirectRequest.target_id == blocker.id,
                        ),
                    ),
                )
                .values(status="CANCELLED", responded_at=utc_now)
            )

            terminated_chat_id: uuid.UUID | None = None
            active_chat = (
                await session.execute(
                    select(ChatSession)
                    .where(
                        ChatSession.status == "ACTIVE",
                        or_(
                            and_(
                                ChatSession.participant_one_id == blocker.id,
                                ChatSession.participant_two_id == blocked.id,
                            ),
                            and_(
                                ChatSession.participant_one_id == blocked.id,
                                ChatSession.participant_two_id == blocker.id,
                            ),
                        ),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()

            if active_chat is not None:
                active_chat.status = "ENDED"
                active_chat.end_reason = "BLOCKED"
                active_chat.ended_at = utc_now
                terminated_chat_id = active_chat.id
                await session.execute(
                    delete(ActiveChatParticipant).where(
                        ActiveChatParticipant.chat_id == active_chat.id
                    )
                )
                if (
                    active_chat.participant_one_sent_msg
                    and active_chat.participant_two_sent_msg
                    and not active_chat.is_charged
                ):
                    await ChatSettlementService(
                        self._db_manager, self._settings
                    ).settle_chat_in_session(session, active_chat, utc_now=utc_now)

            if report_id is not None:
                report = (
                    await session.execute(
                        select(Report).where(Report.id == report_id).with_for_update()
                    )
                ).scalar_one_or_none()
                if report is not None and report.status == "PENDING":
                    report.status = "ACTIONED"
                    report.moderator_id = actor.user_id
                    report.reviewed_at = utc_now
                    report.resolution_notes = clean_reason

            audit_entry = await record_audit_log_in_session(
                session,
                admin_user_id=actor.user_id,
                action="ADMIN_USER_BLOCK",
                target_type="USER_PAIR",
                target_id=f"{blocker.id}:{blocked.id}",
                payload_before={"already_blocked": existing is not None},
                payload_after={
                    "blocker_user_id": blocker.id,
                    "blocked_user_id": blocked.id,
                    "terminated_chat_id": str(terminated_chat_id) if terminated_chat_id else None,
                },
                reason=clean_reason,
                utc_now=utc_now,
            )

            return AdminBlockOutcomeDTO(
                blocked=True,
                blocker_public_id=blocker.public_id,
                blocked_public_id=blocked.public_id,
                audit_log_id=audit_entry.id,
                terminated_chat_id=terminated_chat_id,
                participant_telegram_ids=(
                    (blocker.telegram_user_id, blocked.telegram_user_id)
                    if terminated_chat_id is not None
                    else ()
                ),
            )

    async def admin_unblock_users(
        self,
        actor_telegram_id: int,
        *,
        blocker_public_id: str,
        blocked_public_id: str,
        reason: str,
    ) -> AdminBlockOutcomeDTO:
        """Remove a specific directional UserBlock record with audit logging."""
        clean_reason = reason.strip() if reason else ""
        if not clean_reason:
            raise AdminValidationError("ذکر دلیل برای رفع مسدودیت بین کاربران الزامی است.")

        p1 = blocker_public_id.strip()
        p2 = blocked_public_id.strip()
        if not p1 or not p2 or p1 == p2:
            raise AdminValidationError("دو شناسه عمومی معتبر و متفاوت وارد کنید.")

        utc_now = now_utc()
        async with self._db_manager.session() as session:
            actor = await require_admin_actor_in_session(
                session,
                allowed_roles=MODERATOR_CONTROL_ROLES,
                actor_telegram_id=actor_telegram_id,
            )

            users_rows = (
                (
                    await session.execute(
                        select(User).where(User.public_id.in_([p1, p2]), User.status != "DELETED")
                    )
                )
                .scalars()
                .all()
            )
            by_pid = {u.public_id: u for u in users_rows}
            blocker = by_pid.get(p1)
            blocked = by_pid.get(p2)
            if blocker is None or blocked is None:
                raise AdminNotFoundError("یک یا هر دو کاربر مورد نظر یافت نشدند.")

            existing = (
                await session.execute(
                    select(UserBlock).where(
                        UserBlock.blocker_id == blocker.id,
                        UserBlock.blocked_id == blocked.id,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                raise AdminNotFoundError("رکورد مسدودسازی بین این دو کاربر یافت نشد.")

            old_reason = existing.reason
            await session.delete(existing)
            await session.flush()

            audit_entry = await record_audit_log_in_session(
                session,
                admin_user_id=actor.user_id,
                action="ADMIN_USER_UNBLOCK",
                target_type="USER_PAIR",
                target_id=f"{blocker.id}:{blocked.id}",
                payload_before={"block_reason": old_reason},
                payload_after={"unblocked": True},
                reason=clean_reason,
                utc_now=utc_now,
            )

            return AdminBlockOutcomeDTO(
                blocked=False,
                blocker_public_id=blocker.public_id,
                blocked_public_id=blocked.public_id,
                audit_log_id=audit_entry.id,
            )

    # -------------------------------------------------------------------------
    # Wallet Administration (SUPER_ADMIN only)
    # -------------------------------------------------------------------------
    async def inspect_user_wallet(
        self,
        actor_telegram_id: int,
        target_public_id: str,
        *,
        offset: int = 0,
        limit: int = 5,
    ) -> tuple[AdminUserDetailDTO, WalletBalanceDTO, TransactionHistoryPageDTO]:
        """Inspect a user's wallet balance and paginated ledger for SUPER_ADMIN."""
        clean_pid = target_public_id.strip()
        if not clean_pid:
            raise AdminValidationError("شناسه عمومی کاربر وارد نشده است.")

        async with self._db_manager.session() as session:
            await require_admin_actor_in_session(
                session,
                allowed_roles=SUPER_ADMIN_ONLY_ROLES,
                actor_telegram_id=actor_telegram_id,
            )
            target = (
                await session.execute(
                    select(User).where(User.public_id == clean_pid, User.status != "DELETED")
                )
            ).scalar_one_or_none()
            if target is None:
                raise AdminNotFoundError("کاربر مورد نظر یافت نشد.")
            target_id = target.id

        detail = await self.lookup_user_by_public_id(actor_telegram_id, clean_pid)
        balance = await self._wallet_service.get_wallet_balance(
            target_id, auto_allocate_daily=False
        )
        history = await self._wallet_service.get_transaction_history(
            target_id, offset=offset, limit=limit
        )
        return detail, balance, history

    async def admin_grant_coins(
        self,
        actor_telegram_id: int,
        *,
        target_public_id: str,
        amount: int,
        reason: str,
        idempotency_key: str,
    ) -> AdminWalletOpDTO:
        """Atomically grant purchased Chat Coins and append WalletLedger + AdminAuditLog."""
        clean_pid = target_public_id.strip()
        clean_reason = reason.strip() if reason else ""
        clean_key = idempotency_key.strip() if idempotency_key else ""
        if not clean_pid:
            raise AdminValidationError("شناسه عمومی کاربر الزامی است.")
        if not clean_reason:
            raise AdminValidationError("ذکر دلیل برای افزایش موجودی توسط مدیر الزامی است.")
        if not clean_key:
            raise AdminValidationError("کلید یکتای عملیات (idempotency_key) الزامی است.")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise AdminValidationError("مقدار سکه باید یک عدد صحیح مثبت باشد.")

        utc_now = now_utc()
        async with self._db_manager.session() as session:
            actor = await require_admin_actor_in_session(
                session,
                allowed_roles=SUPER_ADMIN_ONLY_ROLES,
                actor_telegram_id=actor_telegram_id,
            )

            target = (
                await session.execute(
                    select(User).where(User.public_id == clean_pid, User.status != "DELETED")
                )
            ).scalar_one_or_none()
            if target is None:
                raise AdminNotFoundError("کاربر مورد نظر یافت نشد.")

            pre_existing = (
                await session.execute(
                    select(WalletLedger).where(WalletLedger.idempotency_key == clean_key)
                )
            ).scalar_one_or_none()

            wallet_before = await self._wallet_service.ensure_wallet_in_session(
                session, target.id, utc_now=utc_now, lock_for_update=True
            )
            purchased_before = wallet_before.purchased_coins

            balance_dto, ledger_dto = await self._wallet_service.credit_purchased_coins_in_session(
                session,
                target.id,
                amount,
                idempotency_key=clean_key,
                transaction_type="ADMIN_GRANT",
                actor_type="ADMIN",
                actor_id=actor.user_id,
                reference_id=f"admin:{actor.user_id}",
                reason=clean_reason,
                utc_now=utc_now,
            )

            if pre_existing is not None:
                return AdminWalletOpDTO(
                    target_public_id=target.public_id,
                    operation="CREDIT",
                    amount=ledger_dto.amount,
                    purchased_coins_after=balance_dto.purchased_coins,
                    free_coins_after=balance_dto.free_coins,
                    total_spendable_after=balance_dto.total_spendable_coins,
                    ledger_ids=(ledger_dto.id,),
                    audit_log_id=None,
                    already_applied=True,
                )

            audit_entry = await record_audit_log_in_session(
                session,
                admin_user_id=actor.user_id,
                action="WALLET_ADMIN_CREDIT",
                target_type="WALLET",
                target_id=str(target.id),
                payload_before={"purchased_coins": purchased_before},
                payload_after={
                    "purchased_coins": balance_dto.purchased_coins,
                    "amount": amount,
                    "ledger_id": ledger_dto.id,
                    "idempotency_key": clean_key,
                },
                reason=clean_reason,
                utc_now=utc_now,
            )

            return AdminWalletOpDTO(
                target_public_id=target.public_id,
                operation="CREDIT",
                amount=amount,
                purchased_coins_after=balance_dto.purchased_coins,
                free_coins_after=balance_dto.free_coins,
                total_spendable_after=balance_dto.total_spendable_coins,
                ledger_ids=(ledger_dto.id,),
                audit_log_id=audit_entry.id,
                already_applied=False,
            )

    async def admin_debit_coins(
        self,
        actor_telegram_id: int,
        *,
        target_public_id: str,
        amount: int,
        reason: str,
        idempotency_key: str,
        allow_free: bool = False,
    ) -> AdminWalletOpDTO:
        """Atomically debit Chat Coins and append WalletLedger + AdminAuditLog."""
        clean_pid = target_public_id.strip()
        clean_reason = reason.strip() if reason else ""
        clean_key = idempotency_key.strip() if idempotency_key else ""
        if not clean_pid:
            raise AdminValidationError("شناسه عمومی کاربر الزامی است.")
        if not clean_reason:
            raise AdminValidationError("ذکر دلیل برای کسر موجودی توسط مدیر الزامی است.")
        if not clean_key:
            raise AdminValidationError("کلید یکتای عملیات (idempotency_key) الزامی است.")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise AdminValidationError("مقدار کسر سکه باید یک عدد صحیح مثبت باشد.")

        utc_now = now_utc()
        business_date = get_business_date(utc_now, self._wallet_service.business_tz)

        async with self._db_manager.session() as session:
            actor = await require_admin_actor_in_session(
                session,
                allowed_roles=SUPER_ADMIN_ONLY_ROLES,
                actor_telegram_id=actor_telegram_id,
            )

            target = (
                await session.execute(
                    select(User).where(User.public_id == clean_pid, User.status != "DELETED")
                )
            ).scalar_one_or_none()
            if target is None:
                raise AdminNotFoundError("کاربر مورد نظر یافت نشد.")

            pre_existing = (
                await session.execute(
                    select(WalletLedger).where(
                        WalletLedger.idempotency_key.in_(
                            [clean_key, f"{clean_key}:free", f"{clean_key}:purchased"]
                        )
                    )
                )
            ).scalar_one_or_none()

            wallet_before = await self._wallet_service.ensure_wallet_in_session(
                session, target.id, utc_now=utc_now, lock_for_update=True
            )
            allowance_before = await self._wallet_service._get_active_allowance_in_session(
                session, target.id, business_date, lock_for_update=True
            )
            purchased_before = wallet_before.purchased_coins
            free_before = (
                max(0, allowance_before.allocated_credits - allowance_before.used_credits)
                if allowance_before is not None and as_utc(allowance_before.expires_at) > utc_now
                else 0
            )

            balance_dto, ledger_dtos = await self._wallet_service.debit_coins_in_session(
                session,
                target.id,
                amount,
                idempotency_key=clean_key,
                transaction_type="ADMIN_DEBIT",
                actor_type="ADMIN",
                actor_id=actor.user_id,
                reference_id=f"admin:{actor.user_id}",
                reason=clean_reason,
                allow_free=allow_free,
                auto_allocate_daily=False,
                utc_now=utc_now,
            )

            if pre_existing is not None:
                return AdminWalletOpDTO(
                    target_public_id=target.public_id,
                    operation="DEBIT",
                    amount=amount,
                    purchased_coins_after=balance_dto.purchased_coins,
                    free_coins_after=balance_dto.free_coins,
                    total_spendable_after=balance_dto.total_spendable_coins,
                    ledger_ids=tuple(entry.id for entry in ledger_dtos),
                    audit_log_id=None,
                    already_applied=True,
                )

            audit_entry = await record_audit_log_in_session(
                session,
                admin_user_id=actor.user_id,
                action="WALLET_ADMIN_DEBIT",
                target_type="WALLET",
                target_id=str(target.id),
                payload_before={
                    "purchased_coins": purchased_before,
                    "free_coins": free_before,
                },
                payload_after={
                    "purchased_coins": balance_dto.purchased_coins,
                    "free_coins": balance_dto.free_coins,
                    "amount": amount,
                    "ledger_ids": [entry.id for entry in ledger_dtos],
                    "idempotency_key": clean_key,
                },
                reason=clean_reason,
                utc_now=utc_now,
            )

            return AdminWalletOpDTO(
                target_public_id=target.public_id,
                operation="DEBIT",
                amount=amount,
                purchased_coins_after=balance_dto.purchased_coins,
                free_coins_after=balance_dto.free_coins,
                total_spendable_after=balance_dto.total_spendable_coins,
                ledger_ids=tuple(entry.id for entry in ledger_dtos),
                audit_log_id=audit_entry.id,
                already_applied=False,
            )

    # -------------------------------------------------------------------------
    # Role Management (SUPER_ADMIN only)
    # -------------------------------------------------------------------------
    async def list_admin_roles(self, actor_telegram_id: int) -> list[AdminRoleDTO]:
        """List assigned AdminUser rows for SUPER_ADMIN."""
        async with self._db_manager.session() as session:
            await require_admin_actor_in_session(
                session,
                allowed_roles=SUPER_ADMIN_ONLY_ROLES,
                actor_telegram_id=actor_telegram_id,
            )
            rows = (
                await session.execute(
                    select(AdminUser, User)
                    .join(User, User.id == AdminUser.user_id)
                    .order_by(
                        AdminUser.is_active.desc(),
                        AdminUser.role.asc(),
                        AdminUser.user_id.asc(),
                    )
                )
            ).all()

            assigner_ids = {adm.assigned_by for adm, _ in rows if adm.assigned_by is not None}
            assigner_pids: dict[int, str] = {}
            if assigner_ids:
                a_rows = (
                    await session.execute(
                        select(User.id, User.public_id).where(User.id.in_(list(assigner_ids)))
                    )
                ).all()
                assigner_pids = dict(a_rows)

            return [
                AdminRoleDTO(
                    user_id=adm.user_id,
                    public_id=usr.public_id,
                    role=adm.role,
                    is_active=adm.is_active,
                    assigned_by_public_id=(
                        assigner_pids.get(adm.assigned_by) if adm.assigned_by else None
                    ),
                    updated_at=as_utc(adm.updated_at),
                )
                for adm, usr in rows
            ]

    async def assign_or_update_admin_role(
        self,
        actor_telegram_id: int,
        *,
        target_public_id: str,
        role: str,
        reason: str,
    ) -> tuple[AdminRoleDTO, int]:
        """Assign or update an AdminUser role with self-escalation and last-admin guards."""
        norm_role = role.strip().upper()
        if norm_role not in VALID_ADMIN_ROLES:
            raise AdminValidationError("نقش مدیریتی انتخاب‌شده معتبر نیست.")
        clean_reason = reason.strip() if reason else ""
        if not clean_reason:
            raise AdminValidationError("ذکر دلیل برای تغییر نقش مدیریتی الزامی است.")
        clean_pid = target_public_id.strip()
        if not clean_pid:
            raise AdminValidationError("شناسه عمومی کاربر وارد نشده است.")

        utc_now = now_utc()
        async with self._db_manager.session() as session:
            actor = await require_admin_actor_in_session(
                session,
                allowed_roles=SUPER_ADMIN_ONLY_ROLES,
                actor_telegram_id=actor_telegram_id,
            )

            target = (
                await session.execute(
                    select(User).where(User.public_id == clean_pid, User.status != "DELETED")
                )
            ).scalar_one_or_none()
            if target is None:
                raise AdminNotFoundError("کاربر مورد نظر یافت نشد.")
            await reconcile_user_suspension_in_session(session, target, utc_now=utc_now)
            if target.status != "ACTIVE":
                raise AdminConflictError(
                    "امکان اعطای نقش مدیریتی به کاربر تعلیق‌شده یا مسدود وجود ندارد."
                )

            if target.id == actor.user_id:
                raise AdminConflictError("تغییر نقش مدیریتی حساب خودتان مجاز نیست.")

            all_super_admins = (
                (
                    await session.execute(
                        select(AdminUser)
                        .where(AdminUser.role == ROLE_SUPER_ADMIN, AdminUser.is_active.is_(True))
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            existing = (
                await session.execute(
                    select(AdminUser).where(AdminUser.user_id == target.id).with_for_update()
                )
            ).scalar_one_or_none()

            if (
                existing is not None
                and existing.is_active
                and existing.role == ROLE_SUPER_ADMIN
                and norm_role != ROLE_SUPER_ADMIN
                and len(all_super_admins) <= 1
            ):
                raise AdminConflictError("امکان کاهش سطح آخرین سوپر ادمین فعال سامانه وجود ندارد.")

            payload_before = (
                {"role": existing.role, "is_active": existing.is_active}
                if existing is not None
                else None
            )

            if existing is None:
                existing = AdminUser(
                    user_id=target.id,
                    role=norm_role,
                    is_active=True,
                    assigned_by=actor.user_id,
                    created_at=utc_now,
                    updated_at=utc_now,
                )
                session.add(existing)
                action_name = "ADMIN_ROLE_ASSIGN"
            else:
                existing.role = norm_role
                existing.is_active = True
                existing.assigned_by = actor.user_id
                existing.updated_at = utc_now
                action_name = "ADMIN_ROLE_UPDATE"

            await session.flush()

            audit_entry = await record_audit_log_in_session(
                session,
                admin_user_id=actor.user_id,
                action=action_name,
                target_type="ADMIN_USER",
                target_id=str(target.id),
                payload_before=payload_before,
                payload_after={"role": norm_role, "is_active": True},
                reason=clean_reason,
                utc_now=utc_now,
            )

            return (
                AdminRoleDTO(
                    user_id=target.id,
                    public_id=target.public_id,
                    role=existing.role,
                    is_active=existing.is_active,
                    assigned_by_public_id=actor.public_id,
                    updated_at=as_utc(existing.updated_at),
                ),
                audit_entry.id,
            )

    async def revoke_admin_role(
        self,
        actor_telegram_id: int,
        *,
        target_public_id: str,
        reason: str,
    ) -> tuple[AdminRoleDTO, int]:
        """Deactivate an operator's AdminUser access while protecting the last SUPER_ADMIN."""
        clean_reason = reason.strip() if reason else ""
        if not clean_reason:
            raise AdminValidationError("ذکر دلیل برای لغو دسترسی مدیریتی الزامی است.")
        clean_pid = target_public_id.strip()
        if not clean_pid:
            raise AdminValidationError("شناسه عمومی کاربر وارد نشده است.")

        utc_now = now_utc()
        async with self._db_manager.session() as session:
            actor = await require_admin_actor_in_session(
                session,
                allowed_roles=SUPER_ADMIN_ONLY_ROLES,
                actor_telegram_id=actor_telegram_id,
            )

            target = (
                await session.execute(select(User).where(User.public_id == clean_pid))
            ).scalar_one_or_none()
            if target is None:
                raise AdminNotFoundError("کاربر مورد نظر یافت نشد.")

            if target.id == actor.user_id:
                raise AdminConflictError("امکان لغو دسترسی مدیریتی حساب خودتان وجود ندارد.")

            active_supers = (
                (
                    await session.execute(
                        select(AdminUser)
                        .where(AdminUser.role == ROLE_SUPER_ADMIN, AdminUser.is_active.is_(True))
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            admin_row = (
                await session.execute(
                    select(AdminUser).where(AdminUser.user_id == target.id).with_for_update()
                )
            ).scalar_one_or_none()
            if admin_row is None or not admin_row.is_active:
                raise AdminNotFoundError("این کاربر در حال حاضر نقش مدیریتی فعال ندارد.")

            if admin_row.role == ROLE_SUPER_ADMIN and len(active_supers) <= 1:
                raise AdminConflictError(
                    "امکان حذف یا غیرفعال‌سازی آخرین سوپر ادمین فعال وجود ندارد."
                )

            payload_before = {"role": admin_row.role, "is_active": admin_row.is_active}
            admin_row.is_active = False
            admin_row.assigned_by = actor.user_id
            admin_row.updated_at = utc_now
            await session.flush()

            audit_entry = await record_audit_log_in_session(
                session,
                admin_user_id=actor.user_id,
                action="ADMIN_ROLE_REVOKE",
                target_type="ADMIN_USER",
                target_id=str(target.id),
                payload_before=payload_before,
                payload_after={"role": admin_row.role, "is_active": False},
                reason=clean_reason,
                utc_now=utc_now,
            )

            return (
                AdminRoleDTO(
                    user_id=target.id,
                    public_id=target.public_id,
                    role=admin_row.role,
                    is_active=False,
                    assigned_by_public_id=actor.public_id,
                    updated_at=as_utc(admin_row.updated_at),
                ),
                audit_entry.id,
            )

    # -------------------------------------------------------------------------
    # Audit Log Inspection (SUPER_ADMIN only)
    # -------------------------------------------------------------------------
    async def list_audit_logs(
        self,
        actor_telegram_id: int,
        *,
        action_filter: str | None = None,
        offset: int = 0,
        limit: int = 5,
    ) -> AdminAuditLogPageDTO:
        """Return paginated append-only audit log entries for SUPER_ADMIN."""
        safe_offset = max(0, offset)
        safe_limit = max(1, min(limit, 20))
        clean_action = action_filter.strip().upper() if action_filter else None

        async with self._db_manager.session() as session:
            await require_admin_actor_in_session(
                session,
                allowed_roles=SUPER_ADMIN_ONLY_ROLES,
                actor_telegram_id=actor_telegram_id,
            )

            count_stmt = select(func.count(AdminAuditLog.id))
            list_stmt = select(AdminAuditLog)
            if clean_action:
                count_stmt = count_stmt.where(AdminAuditLog.action == clean_action)
                list_stmt = list_stmt.where(AdminAuditLog.action == clean_action)

            total_count = int((await session.execute(count_stmt)).scalar_one())
            rows = (
                (
                    await session.execute(
                        list_stmt.order_by(desc(AdminAuditLog.created_at), desc(AdminAuditLog.id))
                        .offset(safe_offset)
                        .limit(safe_limit)
                    )
                )
                .scalars()
                .all()
            )

            admin_ids = {row.admin_user_id for row in rows}
            pid_map: dict[int, str] = {}
            if admin_ids:
                pid_rows = (
                    await session.execute(
                        select(User.id, User.public_id).where(User.id.in_(list(admin_ids)))
                    )
                ).all()
                pid_map = dict(pid_rows)

            items = tuple(
                AdminAuditLogDTO(
                    id=row.id,
                    admin_public_id=pid_map.get(row.admin_user_id, "usr_admin"),
                    action=row.action,
                    target_type=row.target_type,
                    target_id=row.target_id,
                    payload_before=dict(row.payload_before) if row.payload_before else None,
                    payload_after=dict(row.payload_after) if row.payload_after else None,
                    reason=row.reason,
                    created_at=as_utc(row.created_at),
                )
                for row in rows
            )

            return AdminAuditLogPageDTO(
                items=items,
                total_count=total_count,
                offset=safe_offset,
                limit=safe_limit,
                has_prev=safe_offset > 0,
                has_next=(safe_offset + len(items)) < total_count,
            )
