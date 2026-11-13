"""Anonymous chat session, message relay metadata persistence, and termination service."""

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from src.app.infrastructure.database.base import now_utc
from src.app.infrastructure.database.session import DatabaseManager
from src.app.modules.chat.connections import has_bidirectional_block
from src.app.modules.chat.content_filter import validate_message_text
from src.app.modules.chat.models import ActiveChatParticipant, ChatMessage, ChatSession
from src.app.modules.discovery.schemas import PublicProfileDTO
from src.app.modules.users.models import User

logger = logging.getLogger(__name__)

# Strict database-enforced ceilings (< 10 MiB for video, <= 30 days retention)
VIDEO_SIZE_LIMIT_BYTES = 10 * 1024 * 1024  # 10,485,760 bytes
MESSAGE_RETENTION_DAYS = 30
SUPPORTED_MESSAGE_TYPES = frozenset({"TEXT", "PHOTO", "VOICE", "VIDEO", "STICKER", "ANIMATION"})
VALID_END_REASONS = frozenset(
    {"USER_LEFT", "PARTNER_LEFT", "BLOCKED", "REPORTED", "TIMEOUT_INACTIVE"}
)


@dataclass(frozen=True)
class ActiveChatInfo:
    """Summary of the current active chat for a participant."""

    chat_id: uuid.UUID
    chat_type: str
    own_user_id: int
    own_telegram_id: int
    partner_user_id: int
    partner_telegram_id: int
    partner_profile: PublicProfileDTO


@dataclass(frozen=True)
class RelayMessageResult:
    """Validated message metadata and internal routing info for Telegram delivery."""

    message_id: int
    chat_id: uuid.UUID
    recipient_telegram_id: int
    message_type: str
    text: str | None = None
    telegram_file_id: str | None = None
    caption: str | None = None


@dataclass(frozen=True)
class EndChatResult:
    """Outcome of terminating an active chat session."""

    chat_id: uuid.UUID
    end_reason: str
    actor_user_id: int
    actor_telegram_id: int
    partner_user_id: int
    partner_telegram_id: int


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
        in_active_chat=True,
    )


class ChatService:
    """Manages active anonymous chats, message metadata persistence, and session ending."""

    def __init__(self, db_manager: DatabaseManager) -> None:
        self._db_manager = db_manager

    async def get_active_chat_by_user_id(self, user_id: int) -> ActiveChatInfo | None:
        """Return active chat metadata for user_id if currently in an active chat."""
        async with self._db_manager.session() as session:
            part = (
                await session.execute(
                    select(ActiveChatParticipant).where(ActiveChatParticipant.user_id == user_id)
                )
            ).scalar_one_or_none()
            if part is None:
                return None

            chat = (
                await session.execute(
                    select(ChatSession).where(
                        ChatSession.id == part.chat_id,
                        ChatSession.status == "ACTIVE",
                    )
                )
            ).scalar_one_or_none()
            if chat is None:
                return None

            partner_id = (
                chat.participant_two_id
                if chat.participant_one_id == user_id
                else chat.participant_one_id
            )
            users_res = await session.execute(
                select(User)
                .options(selectinload(User.profile))
                .where(User.id.in_([user_id, partner_id]))
            )
            users_map = {u.id: u for u in users_res.scalars().all()}
            own_user = users_map.get(user_id)
            partner_user = users_map.get(partner_id)
            if own_user is None or partner_user is None or partner_user.profile is None:
                return None

            return ActiveChatInfo(
                chat_id=chat.id,
                chat_type=chat.chat_type,
                own_user_id=own_user.id,
                own_telegram_id=own_user.telegram_user_id,
                partner_user_id=partner_user.id,
                partner_telegram_id=partner_user.telegram_user_id,
                partner_profile=_user_to_public_dto(partner_user),
            )

    async def get_active_chat_by_telegram_id(self, telegram_user_id: int) -> ActiveChatInfo | None:
        """Return active chat metadata for telegram_user_id if currently in an active chat."""
        async with self._db_manager.session() as session:
            uid = (
                await session.execute(
                    select(User.id).where(
                        User.telegram_user_id == telegram_user_id,
                        User.status == "ACTIVE",
                    )
                )
            ).scalar_one_or_none()
            if uid is None:
                return None

        return await self.get_active_chat_by_user_id(uid)

    async def prepare_and_record_message(
        self,
        *,
        sender_user_id: int,
        message_type: str,
        text_content: str | None = None,
        telegram_file_id: str | None = None,
        file_size_bytes: int | None = None,
        duration_seconds: int | None = None,
        caption: str | None = None,
    ) -> RelayMessageResult:
        """Validate message safety & size limits, record metadata, and update participation flags.

        Explicitly leaves chat_sessions.is_charged unchanged (False) per TASK007 wallet boundary.
        Never stores plaintext message body in database or logs.
        """
        norm_type = message_type.upper()
        if norm_type not in SUPPORTED_MESSAGE_TYPES:
            raise ValueError(
                "این نوع پیام در گفتگوی ناشناس پشتیبانی نمی‌شود. "
                "لطفاً پیام متنی، عکس، ویدیو (کمتر از ۱۰ مگابایت)، "
                "پیام صوتی، استیکر یا گیف ارسال کنید."
            )

        if norm_type == "TEXT":
            if not text_content or not text_content.strip():
                raise ValueError("متن پیام نمی‌تواند خالی باشد.")
            filter_res = validate_message_text(text_content)
            if not filter_res.allowed:
                raise PermissionError(filter_res.reason_persian or "محتوای پیام مجاز نیست.")
            hash_source = text_content
        else:
            if not telegram_file_id:
                raise ValueError("شناسه فایل تلگرام نامعتبر است.")
            if norm_type == "VIDEO":
                if file_size_bytes is None or file_size_bytes >= VIDEO_SIZE_LIMIT_BYTES:
                    raise ValueError(
                        "حجم ویدیو باید کمتر از ۱۰ مگابایت باشد. "
                        "ویدیوهای ۱۰ مگابایت یا بیشتر مجاز نیستند."
                    )
            if caption:
                caption_filter = validate_message_text(caption)
                if not caption_filter.allowed:
                    raise PermissionError(
                        caption_filter.reason_persian or "متن همراه فایل مجاز نیست."
                    )
            hash_source = caption or telegram_file_id

        content_hash = hashlib.sha256(hash_source.encode("utf-8")).hexdigest()
        utc_now = now_utc()
        expires_at = utc_now + timedelta(days=MESSAGE_RETENTION_DAYS)

        async with self._db_manager.session() as session:
            part = (
                await session.execute(
                    select(ActiveChatParticipant).where(
                        ActiveChatParticipant.user_id == sender_user_id
                    )
                )
            ).scalar_one_or_none()
            if part is None:
                raise LookupError("شما در حال حاضر در هیچ گفتگوی فعالی حضور ندارید.")

            chat = (
                await session.execute(
                    select(ChatSession).where(ChatSession.id == part.chat_id).with_for_update()
                )
            ).scalar_one_or_none()
            if chat is None or chat.status != "ACTIVE":
                raise LookupError("این گفتگو دیگر فعال نیست.")

            if sender_user_id == chat.participant_one_id:
                recipient_id = chat.participant_two_id
                sender_is_p1 = True
            elif sender_user_id == chat.participant_two_id:
                recipient_id = chat.participant_one_id
                sender_is_p1 = False
            else:
                raise PermissionError("شما عضو این گفتگو نیستید.")

            users_res = await session.execute(
                select(User).where(User.id.in_([sender_user_id, recipient_id]))
            )
            users_map = {u.id: u for u in users_res.scalars().all()}
            sender_user = users_map.get(sender_user_id)
            recipient_user = users_map.get(recipient_id)

            if (
                sender_user is None
                or recipient_user is None
                or sender_user.status != "ACTIVE"
                or recipient_user.status != "ACTIVE"
            ):
                chat.status = "ENDED"
                chat.end_reason = "PARTNER_LEFT"
                chat.ended_at = utc_now
                await session.execute(
                    delete(ActiveChatParticipant).where(ActiveChatParticipant.chat_id == chat.id)
                )
                await session.flush()
                raise RuntimeError("حساب کاربری طرف مقابل غیرفعال است و گفتگو پایان یافت.")

            if await has_bidirectional_block(session, sender_user_id, recipient_id):
                chat.status = "ENDED"
                chat.end_reason = "BLOCKED"
                chat.ended_at = utc_now
                await session.execute(
                    delete(ActiveChatParticipant).where(ActiveChatParticipant.chat_id == chat.id)
                )
                await session.flush()
                raise PermissionError("به دلیل مسدودسازی، این گفتگو پایان یافته است.")

            msg_record = ChatMessage(
                chat_id=chat.id,
                sender_id=sender_user_id,
                recipient_id=recipient_id,
                message_type=norm_type,
                content_hash=content_hash,
                telegram_file_id=telegram_file_id,
                file_size_bytes=file_size_bytes,
                duration_seconds=duration_seconds,
                created_at=utc_now,
                expires_at=expires_at,
            )
            session.add(msg_record)

            if sender_is_p1 and not chat.participant_one_sent_msg:
                chat.participant_one_sent_msg = True
            elif not sender_is_p1 and not chat.participant_two_sent_msg:
                chat.participant_two_sent_msg = True

            # TASK007 wallet boundary: explicitly keep is_charged unchanged (False)
            sender_user.last_activity_at = utc_now

            await session.flush()
            await session.refresh(msg_record)

            return RelayMessageResult(
                message_id=msg_record.id,
                chat_id=chat.id,
                recipient_telegram_id=recipient_user.telegram_user_id,
                message_type=norm_type,
                text=text_content,
                telegram_file_id=telegram_file_id,
                caption=caption,
            )

    async def end_chat(
        self,
        actor_user_id: int,
        *,
        expected_chat_id: uuid.UUID | None = None,
        reason: str = "USER_LEFT",
    ) -> EndChatResult | None:
        """Atomically terminate the active chat for actor_user_id and clear both active rows."""
        clean_reason = reason if reason in VALID_END_REASONS else "USER_LEFT"
        utc_now = now_utc()

        async with self._db_manager.session() as session:
            part = (
                await session.execute(
                    select(ActiveChatParticipant).where(
                        ActiveChatParticipant.user_id == actor_user_id
                    )
                )
            ).scalar_one_or_none()
            if part is None:
                return None

            if expected_chat_id is not None and part.chat_id != expected_chat_id:
                return None

            chat = (
                await session.execute(
                    select(ChatSession).where(ChatSession.id == part.chat_id).with_for_update()
                )
            ).scalar_one_or_none()
            if chat is None:
                await session.execute(
                    delete(ActiveChatParticipant).where(
                        ActiveChatParticipant.chat_id == part.chat_id
                    )
                )
                return None

            if chat.status == "ENDED":
                await session.execute(
                    delete(ActiveChatParticipant).where(ActiveChatParticipant.chat_id == chat.id)
                )
                return None

            chat.status = "ENDED"
            chat.end_reason = clean_reason
            chat.ended_at = utc_now

            await session.execute(
                delete(ActiveChatParticipant).where(ActiveChatParticipant.chat_id == chat.id)
            )

            partner_id = (
                chat.participant_two_id
                if chat.participant_one_id == actor_user_id
                else chat.participant_one_id
            )
            users_res = await session.execute(
                select(User).where(User.id.in_([actor_user_id, partner_id]))
            )
            users_map = {u.id: u for u in users_res.scalars().all()}
            actor_user = users_map.get(actor_user_id)
            partner_user = users_map.get(partner_id)
            if actor_user is not None:
                actor_user.last_activity_at = utc_now

            await session.flush()

            return EndChatResult(
                chat_id=chat.id,
                end_reason=clean_reason,
                actor_user_id=actor_user_id,
                actor_telegram_id=actor_user.telegram_user_id if actor_user else 0,
                partner_user_id=partner_id,
                partner_telegram_id=partner_user.telegram_user_id if partner_user else 0,
            )
