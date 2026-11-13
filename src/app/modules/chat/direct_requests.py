"""Direct chat request lifecycle domain service."""

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from src.app.infrastructure.database.base import now_utc
from src.app.infrastructure.database.session import DatabaseManager
from src.app.modules.chat.connections import ensure_bilateral_connections, has_bidirectional_block
from src.app.modules.chat.models import (
    ActiveChatParticipant,
    ChatSession,
    DirectRequest,
    UserBlock,
)
from src.app.modules.discovery.schemas import PublicProfileDTO
from src.app.modules.users.models import User

logger = logging.getLogger(__name__)

DIRECT_REQUEST_EXPIRY_HOURS = 24


@dataclass(frozen=True)
class DirectRequestDTO:
    """Public-safe direct request summary."""

    id: int
    requester_public_id: str
    target_public_id: str
    peer_profile: PublicProfileDTO
    status: str
    expires_at: object
    created_at: object


@dataclass(frozen=True)
class DirectRequestSendResult:
    """Result of sending a new direct chat request."""

    request_id: int
    target_telegram_id: int
    requester_profile: PublicProfileDTO
    target_profile: PublicProfileDTO
    expires_at: object


@dataclass(frozen=True)
class DirectRequestDecisionResult:
    """Result of accepting or rejecting a direct request."""

    request_id: int
    status: str
    chat_session_id: uuid.UUID | None
    requester_telegram_id: int
    target_telegram_id: int
    requester_profile: PublicProfileDTO
    target_profile: PublicProfileDTO


def _user_to_public_dto(user: User) -> PublicProfileDTO:
    assert user.profile is not None
    return PublicProfileDTO(
        user_id=user.id,
        public_id=user.public_id,
        display_name=user.profile.display_name,
        age=user.profile.age,
        gender=user.profile.gender,
        city=user.profile.city,
        bio=user.profile.bio,
        last_activity_at=user.last_activity_at,
    )


def _is_user_chat_eligible(user: User | None) -> bool:
    return (
        user is not None
        and user.status == "ACTIVE"
        and user.age_18_plus_declared is True
        and user.profile is not None
        and user.profile.completion_status == "COMPLETED"
    )


class DirectRequestService:
    """Manages sending, listing, expiration, acceptance, and rejection of direct requests."""

    def __init__(self, db_manager: DatabaseManager) -> None:
        self._db_manager = db_manager

    async def send_direct_request(
        self,
        requester_user_id: int,
        target_public_id: str,
    ) -> DirectRequestSendResult:
        """Create a new 24-hour PENDING direct request from requester to target."""
        clean_pid = target_public_id.strip()
        if not clean_pid:
            raise ValueError("شناسه عمومی کاربر وارد نشده است.")

        utc_now = now_utc()
        expires_at = utc_now + timedelta(hours=DIRECT_REQUEST_EXPIRY_HOURS)

        async with self._db_manager.session() as session:
            req_res = await session.execute(
                select(User).options(selectinload(User.profile)).where(User.id == requester_user_id)
            )
            requester = req_res.scalar_one_or_none()
            if not _is_user_chat_eligible(requester):
                raise PermissionError(
                    "برای ارسال درخواست مستقیم باید حساب فعال و پروفایل تکمیل‌شده داشته باشید."
                )
            assert requester is not None

            tgt_res = await session.execute(
                select(User).options(selectinload(User.profile)).where(User.public_id == clean_pid)
            )
            target = tgt_res.scalar_one_or_none()
            if not _is_user_chat_eligible(target) or (
                target is not None
                and target.profile is not None
                and target.profile.visibility != "PUBLIC"
            ):
                raise LookupError("کاربر مورد نظر یافت نشد یا پروفایل او در دسترس نیست.")
            assert target is not None

            if requester.id == target.id:
                raise ValueError("امکان ارسال درخواست گفتگو به خودتان وجود ندارد.")

            if await has_bidirectional_block(session, requester.id, target.id):
                raise PermissionError("امکان ارسال درخواست به این کاربر وجود ندارد.")

            # Check whether requester or target is currently in an active chat
            req_active = (
                await session.execute(
                    select(exists().where(ActiveChatParticipant.user_id == requester.id))
                )
            ).scalar_one()
            if req_active:
                raise RuntimeError("شما هم‌اکنون در یک گفتگوی فعال هستید.")

            tgt_active = (
                await session.execute(
                    select(exists().where(ActiveChatParticipant.user_id == target.id))
                )
            ).scalar_one()
            if tgt_active:
                raise RuntimeError("کاربر مورد نظر در حال حاضر در گفتگوی دیگری مشغول است.")

            # Expire any stale pending request in this direction first
            await session.execute(
                update(DirectRequest)
                .where(
                    DirectRequest.requester_id == requester.id,
                    DirectRequest.target_id == target.id,
                    DirectRequest.status == "PENDING",
                    DirectRequest.expires_at <= utc_now,
                )
                .values(status="EXPIRED")
            )

            # Check existing live pending request in the same direction
            dup_res = await session.execute(
                select(DirectRequest.id).where(
                    DirectRequest.requester_id == requester.id,
                    DirectRequest.target_id == target.id,
                    DirectRequest.status == "PENDING",
                    DirectRequest.expires_at > utc_now,
                )
            )
            if dup_res.scalar_one_or_none() is not None:
                raise ValueError("شما قبلاً یک درخواست در انتظار برای این کاربر ارسال کرده‌اید.")

            requester.last_activity_at = utc_now
            direct_req = DirectRequest(
                requester_id=requester.id,
                target_id=target.id,
                status="PENDING",
                expires_at=expires_at,
                created_at=utc_now,
            )
            try:
                async with session.begin_nested():
                    session.add(direct_req)
                    await session.flush()
            except IntegrityError as exc:
                raise ValueError("درخواست در انتظار تکراری برای این کاربر وجود دارد.") from exc

            await session.refresh(direct_req)

            return DirectRequestSendResult(
                request_id=direct_req.id,
                target_telegram_id=target.telegram_user_id,
                requester_profile=_user_to_public_dto(requester),
                target_profile=_user_to_public_dto(target),
                expires_at=direct_req.expires_at,
            )

    async def respond_to_direct_request(
        self,
        request_id: int,
        actor_user_id: int,
        *,
        accept: bool,
    ) -> DirectRequestDecisionResult:
        """Accept or reject a direct request atomically and concurrency-safely.

        When accepted:
        - Marks DirectRequest ACCEPTED.
        - Creates a DIRECT ChatSession with is_charged=False.
        - Inserts both ActiveChatParticipant rows (enforcing 1 active chat per user).
        - Establishes bilateral UserConnection entries.
        """
        utc_now = now_utc()

        error_to_raise: Exception | None = None
        async with self._db_manager.session() as session:
            req_stmt = select(DirectRequest).where(DirectRequest.id == request_id).with_for_update()
            direct_req = (await session.execute(req_stmt)).scalar_one_or_none()
            if direct_req is None:
                raise LookupError("درخواست مورد نظر یافت نشد.")

            if direct_req.target_id != actor_user_id:
                raise PermissionError("شما مجاز به پاسخ‌گویی به این درخواست نیستید.")

            if direct_req.status != "PENDING":
                raise RuntimeError("این درخواست قبلاً تعیین وضعیت شده است.")

            if direct_req.expires_at <= utc_now:
                direct_req.status = "EXPIRED"
                await session.flush()
                error_to_raise = RuntimeError("مهلت ۲۴ ساعته این درخواست به پایان رسیده است.")
            else:
                # Lock users in deterministic ID order to avoid deadlocks under concurrent decisions
                first_id = min(direct_req.requester_id, direct_req.target_id)
                second_id = max(direct_req.requester_id, direct_req.target_id)
                users_res = await session.execute(
                    select(User)
                    .options(selectinload(User.profile))
                    .where(User.id.in_([first_id, second_id]))
                    .order_by(User.id)
                    .with_for_update()
                )
                users_map = {u.id: u for u in users_res.scalars().all()}
                requester = users_map.get(direct_req.requester_id)
                target = users_map.get(direct_req.target_id)

                if not _is_user_chat_eligible(requester) or not _is_user_chat_eligible(target):
                    direct_req.status = "CANCELLED"
                    direct_req.responded_at = utc_now
                    await session.flush()
                    error_to_raise = PermissionError("یکی از کاربران دیگر واجد شرایط گفتگو نیست.")
                elif await has_bidirectional_block(
                    session, direct_req.requester_id, direct_req.target_id
                ):
                    direct_req.status = "CANCELLED"
                    direct_req.responded_at = utc_now
                    await session.flush()
                    error_to_raise = PermissionError(
                        "به دلیل مسدودسازی، امکان برقراری گفتگو وجود ندارد."
                    )

            if error_to_raise is None:
                assert requester is not None and target is not None
                target.last_activity_at = utc_now

                if not accept:
                    direct_req.status = "REJECTED"
                    direct_req.responded_at = utc_now
                    await session.flush()
                    return DirectRequestDecisionResult(
                        request_id=direct_req.id,
                        status="REJECTED",
                        chat_session_id=None,
                        requester_telegram_id=requester.telegram_user_id,
                        target_telegram_id=target.telegram_user_id,
                        requester_profile=_user_to_public_dto(requester),
                        target_profile=_user_to_public_dto(target),
                    )

                # Accepting: ensure neither participant already has an active chat
                active_rows = (
                    (
                        await session.execute(
                            select(ActiveChatParticipant.user_id).where(
                                ActiveChatParticipant.user_id.in_([requester.id, target.id])
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if active_rows:
                    if target.id in active_rows:
                        raise RuntimeError("شما هم‌اکنون در یک گفتگوی فعال هستید.")
                    raise RuntimeError("کاربر درخواست‌دهنده در حال حاضر در گفتگوی دیگری مشغول است.")

                # Transition DirectRequest -> ACCEPTED and flush so composite FK is satisfied
                direct_req.status = "ACCEPTED"
                direct_req.responded_at = utc_now
                await session.flush()

                # Cancel any reverse pending request between the same pair
                await session.execute(
                    update(DirectRequest)
                    .where(
                        DirectRequest.requester_id == target.id,
                        DirectRequest.target_id == requester.id,
                        DirectRequest.status == "PENDING",
                    )
                    .values(status="CANCELLED", responded_at=utc_now)
                )

                chat_id = uuid.uuid4()
                chat_session = ChatSession(
                    id=chat_id,
                    chat_type="DIRECT",
                    direct_request_id=direct_req.id,
                    direct_request_status="ACCEPTED",
                    participant_one_id=direct_req.requester_id,
                    participant_two_id=direct_req.target_id,
                    status="ACTIVE",
                    participant_one_sent_msg=False,
                    participant_two_sent_msg=False,
                    is_charged=False,
                    created_at=utc_now,
                    started_at=utc_now,
                )
                session.add(chat_session)
                await session.flush()

                try:
                    async with session.begin_nested():
                        session.add_all(
                            [
                                ActiveChatParticipant(
                                    user_id=requester.id,
                                    chat_id=chat_id,
                                    joined_at=utc_now,
                                ),
                                ActiveChatParticipant(
                                    user_id=target.id,
                                    chat_id=chat_id,
                                    joined_at=utc_now,
                                ),
                            ]
                        )
                        await session.flush()
                except IntegrityError as exc:
                    raise RuntimeError("امکان شروع هم‌زمان دو گفتگوی فعال وجود ندارد.") from exc

                await ensure_bilateral_connections(
                    session, requester.id, target.id, utc_now=utc_now
                )
                await session.flush()

                return DirectRequestDecisionResult(
                    request_id=direct_req.id,
                    status="ACCEPTED",
                    chat_session_id=chat_id,
                    requester_telegram_id=requester.telegram_user_id,
                    target_telegram_id=target.telegram_user_id,
                    requester_profile=_user_to_public_dto(requester),
                    target_profile=_user_to_public_dto(target),
                )

        if error_to_raise is not None:
            raise error_to_raise
        raise RuntimeError("خطای نامشخص در پردازش درخواست.")

    async def cancel_outgoing_request(
        self,
        request_id: int,
        requester_user_id: int,
    ) -> bool:
        """Allow the original requester to cancel their own pending direct request."""
        utc_now = now_utc()
        async with self._db_manager.session() as session:
            req = (
                await session.execute(
                    select(DirectRequest).where(DirectRequest.id == request_id).with_for_update()
                )
            ).scalar_one_or_none()
            if req is None:
                return False
            if req.requester_id != requester_user_id:
                raise PermissionError("شما مجاز به لغو این درخواست نیستید.")
            if req.status != "PENDING":
                return False
            req.status = "CANCELLED"
            req.responded_at = utc_now
            await session.flush()
            return True

    async def list_incoming_requests(self, target_user_id: int) -> list[DirectRequestDTO]:
        """List non-expired PENDING incoming requests for target_user_id."""
        utc_now = now_utc()
        async with self._db_manager.session() as session:
            # Mark stale expired incoming requests
            await session.execute(
                update(DirectRequest)
                .where(
                    DirectRequest.target_id == target_user_id,
                    DirectRequest.status == "PENDING",
                    DirectRequest.expires_at <= utc_now,
                )
                .values(status="EXPIRED")
            )

            block_exists = exists().where(
                or_(
                    and_(
                        UserBlock.blocker_id == target_user_id,
                        UserBlock.blocked_id == DirectRequest.requester_id,
                    ),
                    and_(
                        UserBlock.blocker_id == DirectRequest.requester_id,
                        UserBlock.blocked_id == target_user_id,
                    ),
                )
            )
            stmt = (
                select(DirectRequest, User)
                .join(User, User.id == DirectRequest.requester_id)
                .options(selectinload(User.profile))
                .where(
                    DirectRequest.target_id == target_user_id,
                    DirectRequest.status == "PENDING",
                    DirectRequest.expires_at > utc_now,
                    User.status == "ACTIVE",
                    ~block_exists,
                )
                .order_by(DirectRequest.created_at.desc())
            )
            rows = (await session.execute(stmt)).all()
            tgt_user = (
                await session.execute(select(User.public_id).where(User.id == target_user_id))
            ).scalar_one_or_none() or ""

            result: list[DirectRequestDTO] = []
            for req, req_user in rows:
                if req_user.profile is None or req_user.profile.completion_status != "COMPLETED":
                    continue
                result.append(
                    DirectRequestDTO(
                        id=req.id,
                        requester_public_id=req_user.public_id,
                        target_public_id=tgt_user,
                        peer_profile=_user_to_public_dto(req_user),
                        status=req.status,
                        expires_at=req.expires_at,
                        created_at=req.created_at,
                    )
                )
            return result

    async def list_sent_requests(self, requester_user_id: int) -> list[DirectRequestDTO]:
        """List non-expired PENDING outgoing requests from requester_user_id."""
        utc_now = now_utc()
        async with self._db_manager.session() as session:
            await session.execute(
                update(DirectRequest)
                .where(
                    DirectRequest.requester_id == requester_user_id,
                    DirectRequest.status == "PENDING",
                    DirectRequest.expires_at <= utc_now,
                )
                .values(status="EXPIRED")
            )

            block_exists = exists().where(
                or_(
                    and_(
                        UserBlock.blocker_id == requester_user_id,
                        UserBlock.blocked_id == DirectRequest.target_id,
                    ),
                    and_(
                        UserBlock.blocker_id == DirectRequest.target_id,
                        UserBlock.blocked_id == requester_user_id,
                    ),
                )
            )
            stmt = (
                select(DirectRequest, User)
                .join(User, User.id == DirectRequest.target_id)
                .options(selectinload(User.profile))
                .where(
                    DirectRequest.requester_id == requester_user_id,
                    DirectRequest.status == "PENDING",
                    DirectRequest.expires_at > utc_now,
                    User.status == "ACTIVE",
                    ~block_exists,
                )
                .order_by(DirectRequest.created_at.desc())
            )
            rows = (await session.execute(stmt)).all()
            req_pid = (
                await session.execute(select(User.public_id).where(User.id == requester_user_id))
            ).scalar_one_or_none() or ""

            result: list[DirectRequestDTO] = []
            for req, tgt_user in rows:
                if tgt_user.profile is None or tgt_user.profile.completion_status != "COMPLETED":
                    continue
                result.append(
                    DirectRequestDTO(
                        id=req.id,
                        requester_public_id=req_pid,
                        target_public_id=tgt_user.public_id,
                        peer_profile=_user_to_public_dto(tgt_user),
                        status=req.status,
                        expires_at=req.expires_at,
                        created_at=req.created_at,
                    )
                )
            return result
