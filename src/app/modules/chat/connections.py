"""Connections address book and bidirectional user blocking domain service."""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, delete, exists, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.infrastructure.database.base import now_utc
from src.app.infrastructure.database.session import DatabaseManager
from src.app.modules.chat.models import (
    ActiveChatParticipant,
    ChatSession,
    DirectRequest,
    UserBlock,
    UserConnection,
)
from src.app.modules.users.models import User, UserProfile

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConnectionItemDTO:
    """Public-safe representation of an established contact connection."""

    connection_id: int
    public_id: str
    display_name: str
    age: int
    gender: str
    city: str | None
    bio: str | None
    last_activity_at: datetime
    established_at: datetime
    is_public: bool
    in_active_chat: bool


@dataclass(frozen=True)
class BlockResult:
    """Outcome of blocking another user, including any terminated active chat."""

    blocked: bool
    already_blocked: bool
    target_public_id: str
    terminated_chat_id: uuid.UUID | None = None
    terminated_partner_telegram_id: int | None = None
    blocker_telegram_id: int | None = None


async def ensure_bilateral_connections(
    session: AsyncSession,
    user_a_id: int,
    user_b_id: int,
    *,
    utc_now: datetime | None = None,
) -> None:
    """Ensure active UserConnection rows exist for both directions between two users."""
    ts = utc_now or now_utc()
    dialect_name = session.bind.dialect.name if session.bind is not None else ""

    for owner_id, peer_id in ((user_a_id, user_b_id), (user_b_id, user_a_id)):
        if dialect_name == "postgresql":
            stmt = (
                pg_insert(UserConnection)
                .values(
                    user_id=owner_id,
                    connected_user_id=peer_id,
                    status="ACTIVE",
                    established_at=ts,
                    updated_at=ts,
                )
                .on_conflict_do_update(
                    constraint="uq_user_connections_pair",
                    set_={"status": "ACTIVE", "updated_at": ts},
                )
            )
            await session.execute(stmt)
        else:
            existing_res = await session.execute(
                select(UserConnection).where(
                    UserConnection.user_id == owner_id,
                    UserConnection.connected_user_id == peer_id,
                )
            )
            existing = existing_res.scalar_one_or_none()
            if existing is None:
                session.add(
                    UserConnection(
                        user_id=owner_id,
                        connected_user_id=peer_id,
                        status="ACTIVE",
                        established_at=ts,
                        updated_at=ts,
                    )
                )
            elif existing.status != "ACTIVE":
                existing.status = "ACTIVE"
                existing.updated_at = ts


async def has_bidirectional_block(
    session: AsyncSession,
    user_a_id: int,
    user_b_id: int,
) -> bool:
    """Return True if either user has blocked the other."""
    res = await session.execute(
        select(
            exists().where(
                or_(
                    and_(UserBlock.blocker_id == user_a_id, UserBlock.blocked_id == user_b_id),
                    and_(UserBlock.blocker_id == user_b_id, UserBlock.blocked_id == user_a_id),
                )
            )
        )
    )
    return bool(res.scalar_one())


class ConnectionAndBlockService:
    """Manages persistent user connections and bidirectional user blocks."""

    def __init__(self, db_manager: DatabaseManager) -> None:
        self._db_manager = db_manager

    async def is_blocked(self, user_a_id: int, user_b_id: int) -> bool:
        """Check whether a block exists in either direction between two users."""
        async with self._db_manager.session() as session:
            return await has_bidirectional_block(session, user_a_id, user_b_id)

    async def list_connections(self, user_id: int) -> list[ConnectionItemDTO]:
        """List active connections for a user, excluding blocked or non-active accounts."""
        async with self._db_manager.session() as session:
            block_exists = exists().where(
                or_(
                    and_(UserBlock.blocker_id == user_id, UserBlock.blocked_id == User.id),
                    and_(UserBlock.blocker_id == User.id, UserBlock.blocked_id == user_id),
                )
            )
            active_chat_exists = exists().where(ActiveChatParticipant.user_id == User.id)

            stmt = (
                select(
                    UserConnection,
                    User,
                    UserProfile,
                    active_chat_exists.label("in_active_chat"),
                )
                .join(User, User.id == UserConnection.connected_user_id)
                .join(UserProfile, UserProfile.user_id == User.id)
                .where(
                    UserConnection.user_id == user_id,
                    UserConnection.status == "ACTIVE",
                    User.status == "ACTIVE",
                    User.age_18_plus_declared.is_(True),
                    UserProfile.completion_status == "COMPLETED",
                    ~block_exists,
                )
                .order_by(UserConnection.updated_at.desc(), UserConnection.id.desc())
            )
            rows = (await session.execute(stmt)).all()
            return [
                ConnectionItemDTO(
                    connection_id=conn.id,
                    public_id=peer_user.public_id,
                    display_name=peer_profile.display_name,
                    age=peer_profile.age,
                    gender=peer_profile.gender,
                    city=peer_profile.city,
                    bio=peer_profile.bio,
                    last_activity_at=peer_user.last_activity_at,
                    established_at=conn.established_at,
                    is_public=(peer_profile.visibility == "PUBLIC"),
                    in_active_chat=bool(in_active),
                )
                for conn, peer_user, peer_profile, in_active in rows
            ]

    async def remove_connection(self, user_id: int, target_public_id: str) -> bool:
        """Asymmetrically remove a connection from the acting user's contact list only.

        Does NOT modify the peer user's connection record and does NOT create a block.
        """
        clean_pid = target_public_id.strip()
        if not clean_pid:
            return False

        utc_now = now_utc()
        async with self._db_manager.session() as session:
            target_res = await session.execute(select(User.id).where(User.public_id == clean_pid))
            target_id = target_res.scalar_one_or_none()
            if target_id is None:
                return False

            res = await session.execute(
                update(UserConnection)
                .where(
                    UserConnection.user_id == user_id,
                    UserConnection.connected_user_id == target_id,
                    UserConnection.status == "ACTIVE",
                )
                .values(status="REMOVED", updated_at=utc_now)
            )
            return res.rowcount > 0

    async def block_user(
        self,
        blocker_user_id: int,
        target_public_id: str,
        *,
        reason: str | None = None,
    ) -> BlockResult:
        """Create a block against target user, cancel requests, and terminate active chat."""
        clean_pid = target_public_id.strip()
        utc_now = now_utc()

        async with self._db_manager.session() as session:
            blocker_res = await session.execute(
                select(User).where(User.id == blocker_user_id, User.status == "ACTIVE")
            )
            blocker = blocker_res.scalar_one_or_none()
            if blocker is None:
                raise PermissionError("کاربر یافت نشد یا غیرفعال است.")

            target_res = await session.execute(
                select(User).where(User.public_id == clean_pid, User.status != "DELETED")
            )
            target = target_res.scalar_one_or_none()
            if target is None:
                raise LookupError("کاربر مورد نظر یافت نشد.")
            if target.id == blocker.id:
                raise ValueError("امکان مسدود کردن خودتان وجود ندارد.")

            existing_block_res = await session.execute(
                select(UserBlock).where(
                    UserBlock.blocker_id == blocker.id,
                    UserBlock.blocked_id == target.id,
                )
            )
            existing_block = existing_block_res.scalar_one_or_none()
            already_blocked = existing_block is not None
            if existing_block is None:
                session.add(
                    UserBlock(
                        blocker_id=blocker.id,
                        blocked_id=target.id,
                        reason=reason[:100] if reason else None,
                        created_at=utc_now,
                    )
                )
                await session.flush()

            # Cancel any pending direct requests in both directions
            await session.execute(
                update(DirectRequest)
                .where(
                    DirectRequest.status == "PENDING",
                    or_(
                        and_(
                            DirectRequest.requester_id == blocker.id,
                            DirectRequest.target_id == target.id,
                        ),
                        and_(
                            DirectRequest.requester_id == target.id,
                            DirectRequest.target_id == blocker.id,
                        ),
                    ),
                )
                .values(status="CANCELLED", responded_at=utc_now)
            )

            # Terminate any shared active chat immediately
            terminated_chat_id: uuid.UUID | None = None
            active_chat_res = await session.execute(
                select(ChatSession)
                .where(
                    ChatSession.status == "ACTIVE",
                    or_(
                        and_(
                            ChatSession.participant_one_id == blocker.id,
                            ChatSession.participant_two_id == target.id,
                        ),
                        and_(
                            ChatSession.participant_one_id == target.id,
                            ChatSession.participant_two_id == blocker.id,
                        ),
                    ),
                )
                .with_for_update()
            )
            active_chat = active_chat_res.scalar_one_or_none()
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

            blocker.last_activity_at = utc_now
            await session.flush()

            return BlockResult(
                blocked=True,
                already_blocked=already_blocked,
                target_public_id=target.public_id,
                terminated_chat_id=terminated_chat_id,
                terminated_partner_telegram_id=(
                    target.telegram_user_id if terminated_chat_id is not None else None
                ),
                blocker_telegram_id=blocker.telegram_user_id,
            )

    async def block_active_chat_partner(
        self,
        blocker_user_id: int,
        chat_id: uuid.UUID,
        *,
        reason: str | None = "CHAT_BLOCK",
    ) -> BlockResult:
        """Block the other participant of a specific active chat session."""
        async with self._db_manager.session() as session:
            chat_res = await session.execute(
                select(ChatSession).where(
                    ChatSession.id == chat_id,
                    ChatSession.status == "ACTIVE",
                    or_(
                        ChatSession.participant_one_id == blocker_user_id,
                        ChatSession.participant_two_id == blocker_user_id,
                    ),
                )
            )
            chat = chat_res.scalar_one_or_none()
            if chat is None:
                raise LookupError("گفتگوی فعال مورد نظر یافت نشد.")

            partner_id = (
                chat.participant_two_id
                if chat.participant_one_id == blocker_user_id
                else chat.participant_one_id
            )
            partner_res = await session.execute(select(User.public_id).where(User.id == partner_id))
            partner_public_id = partner_res.scalar_one()

        return await self.block_user(
            blocker_user_id=blocker_user_id,
            target_public_id=partner_public_id,
            reason=reason,
        )
