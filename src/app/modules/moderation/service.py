"""User reporting and evidence-hold metadata preservation domain service."""

import hashlib
import logging
import uuid
from datetime import timedelta

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.infrastructure.database.base import now_utc
from src.app.infrastructure.database.session import DatabaseManager
from src.app.modules.chat.models import (
    ActiveChatParticipant,
    ChatMessage,
    ChatSession,
    MessageEvidenceHold,
)
from src.app.modules.moderation.enforcement import as_utc, reconcile_user_suspension_in_session
from src.app.modules.moderation.models import Report
from src.app.modules.moderation.schemas import (
    MAX_REPORT_DESCRIPTION_LENGTH,
    REPORT_CATEGORIES,
    REPORT_COOLDOWN_MINUTES,
    REPORT_EVIDENCE_MESSAGE_LIMIT,
    ReportSubmissionResult,
)
from src.app.modules.users.models import User

logger = logging.getLogger(__name__)


def validate_report_category(category: str) -> str:
    """Validate and normalize report category against DB constraint."""
    norm = category.strip().upper()
    if norm not in REPORT_CATEGORIES:
        raise ValueError("دسته‌بندی گزارش تخلف نامعتبر است.")
    return norm


def validate_report_description(description: str | None) -> str | None:
    """Sanitize optional user-supplied report explanation."""
    if description is None:
        return None
    cleaned = description.strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_REPORT_DESCRIPTION_LENGTH:
        raise ValueError(
            f"توضیحات گزارش نمی‌تواند بیش از {MAX_REPORT_DESCRIPTION_LENGTH} کاراکتر باشد."
        )
    return cleaned


class ReportService:
    """Manages user abuse report submission and metadata-only evidence holds."""

    def __init__(self, db_manager: DatabaseManager) -> None:
        self._db_manager = db_manager

    async def submit_report(
        self,
        *,
        reporter_user_id: int,
        category: str,
        reported_public_id: str | None = None,
        reported_user_id: int | None = None,
        chat_session_id: uuid.UUID | None = None,
        reported_message_id: int | None = None,
        description: str | None = None,
    ) -> ReportSubmissionResult:
        """Create a PENDING report with duplicate protection and evidence-hold metadata.

        Never stores plaintext message bodies in reports or evidence holds.
        """
        norm_category = validate_report_category(category)
        clean_desc = validate_report_description(description)
        utc_now = now_utc()

        async with self._db_manager.session() as session:
            reporter = (
                await session.execute(select(User).where(User.id == reporter_user_id))
            ).scalar_one_or_none()
            if reporter is None:
                raise PermissionError("حساب کاربری گزارش‌دهنده یافت نشد.")
            await reconcile_user_suspension_in_session(session, reporter, utc_now=utc_now)
            if reporter.status != "ACTIVE" or not reporter.age_18_plus_declared:
                raise PermissionError("فقط کاربران فعال و تأییدشده مجاز به ثبت گزارش هستند.")

            target: User | None = None
            if reported_user_id is not None:
                target = (
                    await session.execute(
                        select(User).where(User.id == reported_user_id, User.status != "DELETED")
                    )
                ).scalar_one_or_none()
            elif reported_public_id and reported_public_id.strip():
                target = (
                    await session.execute(
                        select(User).where(
                            User.public_id == reported_public_id.strip(),
                            User.status != "DELETED",
                        )
                    )
                ).scalar_one_or_none()
            else:
                raise ValueError("شناسه کاربر گزارش‌شده مشخص نشده است.")

            if target is None:
                raise LookupError("کاربر مورد نظر یافت نشد.")
            if target.id == reporter.id:
                raise ValueError("امکان ثبت گزارش علیه خودتان وجود ندارد.")

            resolved_chat_id = chat_session_id
            resolved_msg: ChatMessage | None = None

            if reported_message_id is not None:
                resolved_msg = (
                    await session.execute(
                        select(ChatMessage).where(ChatMessage.id == reported_message_id)
                    )
                ).scalar_one_or_none()
                if resolved_msg is None:
                    raise LookupError("پیام مورد نظر برای گزارش یافت نشد.")
                if resolved_chat_id is not None and resolved_msg.chat_id != resolved_chat_id:
                    raise ValueError("پیام انتخاب‌شده متعلق به این جلسه گفتگو نیست.")
                resolved_chat_id = resolved_msg.chat_id
                if {resolved_msg.sender_id, resolved_msg.recipient_id} != {reporter.id, target.id}:
                    raise PermissionError("شما مجاز به گزارش پیام متعلق به گفتگوی دیگران نیستید.")

            if resolved_chat_id is not None:
                chat = (
                    await session.execute(
                        select(ChatSession).where(ChatSession.id == resolved_chat_id)
                    )
                ).scalar_one_or_none()
                if chat is None:
                    raise LookupError("جلسه گفتگوی مورد نظر یافت نشد.")
                if {chat.participant_one_id, chat.participant_two_id} != {reporter.id, target.id}:
                    raise PermissionError("شما عضو این جلسه گفتگو نیستید.")

            await self._check_duplicate_or_flood(
                session,
                reporter_id=reporter.id,
                reported_user_id=target.id,
                chat_session_id=resolved_chat_id,
                utc_now=utc_now,
            )

            report = Report(
                reporter_id=reporter.id,
                reported_user_id=target.id,
                chat_session_id=resolved_chat_id,
                reported_message_id=resolved_msg.id if resolved_msg is not None else None,
                category=norm_category,
                description=clean_desc,
                status="PENDING",
                created_at=utc_now,
            )
            session.add(report)
            await session.flush()
            await session.refresh(report)

            holds_created = 0
            if resolved_chat_id is not None:
                holds_created = await self._create_user_report_evidence_holds(
                    session,
                    report=report,
                    chat_session_id=resolved_chat_id,
                    reported_user_id=target.id,
                    specific_message=resolved_msg,
                    utc_now=utc_now,
                )

            reporter.last_activity_at = utc_now
            await session.flush()

            logger.info(
                "Submitted report_id=%s reporter=%s target=%s category=%s holds=%s",
                report.id,
                reporter.id,
                target.id,
                norm_category,
                holds_created,
            )

            return ReportSubmissionResult(
                report_id=report.id,
                status=report.status,
                category=report.category,
                reported_public_id=target.public_id,
                chat_session_id=report.chat_session_id,
                evidence_holds_created=holds_created,
                created_at=as_utc(report.created_at),
            )

    async def submit_active_chat_report(
        self,
        *,
        reporter_user_id: int,
        chat_session_id: uuid.UUID | None,
        category: str,
        description: str | None = None,
    ) -> ReportSubmissionResult:
        """Resolve current or specified chat partner safely and submit a chat report."""
        async with self._db_manager.session() as session:
            target_chat_id = chat_session_id
            if target_chat_id is None:
                part = (
                    await session.execute(
                        select(ActiveChatParticipant).where(
                            ActiveChatParticipant.user_id == reporter_user_id
                        )
                    )
                ).scalar_one_or_none()
                if part is None:
                    raise LookupError("گفتگوی فعالی برای ثبت گزارش یافت نشد.")
                target_chat_id = part.chat_id

            chat = (
                await session.execute(select(ChatSession).where(ChatSession.id == target_chat_id))
            ).scalar_one_or_none()
            if chat is None:
                raise LookupError("جلسه گفتگوی مورد نظر یافت نشد.")

            if chat.participant_one_id == reporter_user_id:
                partner_id = chat.participant_two_id
            elif chat.participant_two_id == reporter_user_id:
                partner_id = chat.participant_one_id
            else:
                raise PermissionError("شما عضو این جلسه گفتگو نیستید.")

        return await self.submit_report(
            reporter_user_id=reporter_user_id,
            reported_user_id=partner_id,
            chat_session_id=target_chat_id,
            category=category,
            description=description,
        )

    @staticmethod
    async def _check_duplicate_or_flood(
        session: AsyncSession,
        *,
        reporter_id: int,
        reported_user_id: int,
        chat_session_id: uuid.UUID | None,
        utc_now,
    ) -> None:
        pending_stmt = select(Report.id).where(
            Report.reporter_id == reporter_id,
            Report.reported_user_id == reported_user_id,
            Report.status == "PENDING",
        )
        if chat_session_id is not None:
            pending_stmt = pending_stmt.where(Report.chat_session_id == chat_session_id)
        else:
            pending_stmt = pending_stmt.where(Report.chat_session_id.is_(None))

        if (await session.execute(pending_stmt.limit(1))).scalar_one_or_none() is not None:
            raise ValueError("گزارش قبلی شما برای این مورد همچنان در صف بررسی است.")

        cooldown_cutoff = utc_now - timedelta(minutes=REPORT_COOLDOWN_MINUTES)
        recent_stmt = select(Report.id).where(
            Report.reporter_id == reporter_id,
            Report.reported_user_id == reported_user_id,
            Report.created_at >= cooldown_cutoff,
        )
        if chat_session_id is not None:
            recent_stmt = recent_stmt.where(Report.chat_session_id == chat_session_id)
        if (await session.execute(recent_stmt.limit(1))).scalar_one_or_none() is not None:
            raise ValueError("شما به تازگی گزارشی برای این کاربر ثبت کرده‌اید. لطفاً کمی صبر کنید.")

    @staticmethod
    async def _create_user_report_evidence_holds(
        session: AsyncSession,
        *,
        report: Report,
        chat_session_id: uuid.UUID,
        reported_user_id: int,
        specific_message: ChatMessage | None,
        utc_now,
    ) -> int:
        messages: list[ChatMessage] = []
        if specific_message is not None:
            messages = [specific_message]
        else:
            stmt = (
                select(ChatMessage)
                .where(
                    ChatMessage.chat_id == chat_session_id,
                    ChatMessage.sender_id == reported_user_id,
                )
                .order_by(desc(ChatMessage.created_at), desc(ChatMessage.id))
                .limit(REPORT_EVIDENCE_MESSAGE_LIMIT)
            )
            messages = list((await session.execute(stmt)).scalars().all())
            if not messages:
                # Fall back to latest session messages if reported user sent none yet
                fallback_stmt = (
                    select(ChatMessage)
                    .where(ChatMessage.chat_id == chat_session_id)
                    .order_by(desc(ChatMessage.created_at), desc(ChatMessage.id))
                    .limit(REPORT_EVIDENCE_MESSAGE_LIMIT)
                )
                messages = list((await session.execute(fallback_stmt)).scalars().all())

        created = 0
        hold_reason = f"User report ({report.category}) metadata preservation"
        for msg in messages:
            if not msg.content_hash:
                continue
            session.add(
                MessageEvidenceHold(
                    message_id=msg.id,
                    chat_session_id=chat_session_id,
                    message_content_hash=msg.content_hash,
                    report_id=report.id,
                    legal_reference_id=None,
                    hold_type="USER_REPORT",
                    status="ACTIVE",
                    reason=hold_reason,
                    created_by_admin_id=None,
                    created_by_system_actor="REPORT_SYSTEM",
                    created_at=utc_now,
                )
            )
            created += 1

        if created == 0:
            # Always record metadata-only evidence hold linking report to session
            session_hash = hashlib.sha256(
                f"report:{report.id}:session:{chat_session_id}".encode()
            ).hexdigest()
            session.add(
                MessageEvidenceHold(
                    message_id=None,
                    chat_session_id=chat_session_id,
                    message_content_hash=session_hash,
                    report_id=report.id,
                    legal_reference_id=None,
                    hold_type="USER_REPORT",
                    status="ACTIVE",
                    reason=hold_reason,
                    created_by_admin_id=None,
                    created_by_system_actor="REPORT_SYSTEM",
                    created_at=utc_now,
                )
            )
            created = 1

        await session.flush()
        return created
