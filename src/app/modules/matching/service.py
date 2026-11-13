"""Random matchmaking queue and concurrency-safe pairing domain service."""

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, desc, exists, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from src.app.infrastructure.database.base import now_utc
from src.app.infrastructure.database.session import DatabaseManager
from src.app.infrastructure.redis.client import RedisManager
from src.app.modules.chat.connections import ensure_bilateral_connections, has_bidirectional_block
from src.app.modules.chat.models import ActiveChatParticipant, ChatSession, UserBlock
from src.app.modules.discovery.schemas import PublicProfileDTO, UserPreferencesDTO
from src.app.modules.discovery.service import (
    preferences_model_to_dto,
    profile_matches_preferences,
)
from src.app.modules.moderation.enforcement import (
    has_active_chat_restriction_in_session,
    reconcile_user_suspension_in_session,
)
from src.app.modules.users.models import User, UserPreferences

logger = logging.getLogger(__name__)

QUEUE_TTL_SECONDS = 3600
RECENT_PAIR_WINDOW_HOURS = 24
RECENT_PAIR_HISTORY_LIMIT = 5


@dataclass(frozen=True)
class MatchResult:
    """Outcome of entering or checking the random matchmaking queue."""

    status: str  # 'MATCHED', 'QUEUED', 'ALREADY_IN_CHAT'
    chat_session_id: uuid.UUID | None = None
    seeker_telegram_id: int | None = None
    partner_telegram_id: int | None = None
    seeker_profile: PublicProfileDTO | None = None
    partner_profile: PublicProfileDTO | None = None
    queue_size: int = 0
    matched_via_relaxed: bool = False


def is_mutual_match(
    prefs_a: UserPreferencesDTO,
    profile_a: PublicProfileDTO,
    prefs_b: UserPreferencesDTO,
    profile_b: PublicProfileDTO,
    *,
    allow_relaxed: bool = False,
    now_ts: Any | None = None,
) -> bool:
    """Return True if user A and user B mutually satisfy each other's preferences.

    Strict filters are never relaxed. Relaxable filters are only widened when
    allow_relaxed=True AND explicitly permitted in that user's relaxable_filters.
    """
    return profile_matches_preferences(
        prefs_a, profile_b, allow_relaxed=allow_relaxed, now_ts=now_ts
    ) and profile_matches_preferences(
        prefs_b, profile_a, allow_relaxed=allow_relaxed, now_ts=now_ts
    )


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


class RandomMatchService:
    """Coordinates random matchmaking using Redis queues and PostgreSQL ACID authority."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        redis_manager: RedisManager | None = None,
        *,
        queue_key: str = "matching:queue",
    ) -> None:
        self._db_manager = db_manager
        self._redis_manager = redis_manager
        self._queue_key = queue_key
        # Fallback in-memory queue strictly for Redis-less unit tests
        self._local_queue: dict[int, float] = {}

    async def is_in_queue(self, user_id: int) -> bool:
        """Return True if user is currently waiting in queue (and not in an active chat)."""
        async with self._db_manager.session() as session:
            in_chat = (
                await session.execute(
                    select(exists().where(ActiveChatParticipant.user_id == user_id))
                )
            ).scalar_one()
            if in_chat:
                await self._queue_remove(user_id)
                return False

        return await self._queue_contains(user_id)

    async def leave_queue(self, user_id: int) -> bool:
        """Remove a user from the random matching queue."""
        return await self._queue_remove(user_id)

    async def get_queue_size(self) -> int:
        """Return current count of waiting candidates in queue."""
        entries = await self._queue_get_all()
        return len(entries)

    async def enter_queue_and_find_match(self, user_id: int) -> MatchResult:
        """Attempt to match user_id with an eligible waiting partner, else enqueue them.

        PostgreSQL transactions and ActiveChatParticipant primary keys provide the final
        concurrency-safe guarantee against duplicate active sessions.
        """
        utc_now = now_utc()

        # 1. Validate seeker eligibility, completed profile, and not in active chat
        async with self._db_manager.session() as session:
            seeker = (
                await session.execute(
                    select(User)
                    .options(selectinload(User.profile), selectinload(User.preferences))
                    .where(User.id == user_id)
                )
            ).scalar_one_or_none()

            await reconcile_user_suspension_in_session(session, seeker, utc_now=utc_now)

            if (
                seeker is None
                or seeker.status != "ACTIVE"
                or not seeker.age_18_plus_declared
                or seeker.profile is None
                or seeker.profile.completion_status != "COMPLETED"
            ):
                await self._queue_remove(user_id)
                raise PermissionError(
                    "برای استفاده از گفتگوی تصادفی باید حساب فعال و پروفایل تکمیل‌شده داشته باشید."
                )

            if await has_active_chat_restriction_in_session(session, seeker.id, utc_now=utc_now):
                await self._queue_remove(user_id)
                raise PermissionError("دسترسی گفتگوی شما توسط تیم نظارت محدود شده است.")

            in_chat = (
                await session.execute(
                    select(exists().where(ActiveChatParticipant.user_id == user_id))
                )
            ).scalar_one()
            if in_chat:
                await self._queue_remove(user_id)
                return MatchResult(status="ALREADY_IN_CHAT")

            seeker.last_activity_at = utc_now
            seeker_dto = _user_to_public_dto(seeker)
            seeker_prefs = preferences_model_to_dto(seeker.id, seeker.preferences)

        # 2. Fetch candidate queue entries (FIFO ordered)
        now_ts = time.time()
        await self._prune_stale_queue_entries(now_ts - QUEUE_TTL_SECONDS)
        queue_entries = await self._queue_get_all()
        candidate_scores = {cid: score for cid, score in queue_entries if cid != user_id}

        if candidate_scores:
            ranked_candidates = await self._rank_compatible_candidates(
                seeker_id=user_id,
                seeker_dto=seeker_dto,
                seeker_prefs=seeker_prefs,
                candidate_scores=candidate_scores,
                utc_now=utc_now,
            )

            for cand_id, used_relaxed in ranked_candidates:
                paired = await self._try_create_random_session(
                    seeker_id=user_id,
                    partner_id=cand_id,
                    seeker_prefs=seeker_prefs,
                    used_relaxed=used_relaxed,
                )
                if paired is not None:
                    if paired.status == "ALREADY_IN_CHAT":
                        await self._queue_remove(user_id)
                    elif paired.status == "MATCHED":
                        await self._queue_remove(user_id)
                        await self._queue_remove(cand_id)
                    return paired

        # 3. No compatible waiting partner right now -> ensure user is queued
        # Double-check user wasn't matched concurrently before adding to queue
        async with self._db_manager.session() as session:
            in_chat_after = (
                await session.execute(
                    select(exists().where(ActiveChatParticipant.user_id == user_id))
                )
            ).scalar_one()
            if in_chat_after:
                await self._queue_remove(user_id)
                return MatchResult(status="ALREADY_IN_CHAT")

        await self._queue_add(user_id, now_ts)
        q_size = await self.get_queue_size()
        return MatchResult(status="QUEUED", queue_size=q_size)

    async def _rank_compatible_candidates(
        self,
        *,
        seeker_id: int,
        seeker_dto: PublicProfileDTO,
        seeker_prefs: UserPreferencesDTO,
        candidate_scores: dict[int, float],
        utc_now: Any,
    ) -> list[tuple[int, bool]]:
        """Filter queued candidates in PostgreSQL and rank exact/fresh matches first."""
        cand_ids = list(candidate_scores.keys())
        stale_to_evict: list[int] = []

        async with self._db_manager.session() as session:
            block_exists = exists().where(
                or_(
                    and_(UserBlock.blocker_id == seeker_id, UserBlock.blocked_id == User.id),
                    and_(UserBlock.blocker_id == User.id, UserBlock.blocked_id == seeker_id),
                )
            )
            active_exists = exists().where(ActiveChatParticipant.user_id == User.id)

            stmt = (
                select(
                    User,
                    active_exists.label("in_active_chat"),
                    block_exists.label("is_blocked"),
                )
                .options(selectinload(User.profile), selectinload(User.preferences))
                .where(User.id.in_(cand_ids))
            )
            rows = (await session.execute(stmt)).all()

            recent_cutoff = utc_now - timedelta(hours=RECENT_PAIR_WINDOW_HOURS)
            recent_stmt = (
                select(ChatSession.participant_one_id, ChatSession.participant_two_id)
                .where(
                    ChatSession.status == "ENDED",
                    or_(
                        ChatSession.participant_one_id == seeker_id,
                        ChatSession.participant_two_id == seeker_id,
                    ),
                    ChatSession.created_at >= recent_cutoff,
                )
                .order_by(desc(ChatSession.created_at))
                .limit(RECENT_PAIR_HISTORY_LIMIT)
            )
            recent_rows = (await session.execute(recent_stmt)).all()
            recent_partner_ids: set[int] = set()
            for p1, p2 in recent_rows:
                recent_partner_ids.add(p2 if p1 == seeker_id else p1)

            found_ids: set[int] = set()
            # Sort tuples: (relaxed_tier, recent_tier, joined_timestamp, cand_id)
            scored_matches: list[tuple[int, int, float, int, bool]] = []

            for cand_user, in_active_chat, is_blocked in rows:
                found_ids.add(cand_user.id)
                await reconcile_user_suspension_in_session(session, cand_user, utc_now=utc_now)
                cand_restricted = await has_active_chat_restriction_in_session(
                    session, cand_user.id, utc_now=utc_now
                )
                if (
                    cand_user.status != "ACTIVE"
                    or not cand_user.age_18_plus_declared
                    or cand_user.profile is None
                    or cand_user.profile.completion_status != "COMPLETED"
                    or in_active_chat
                    or cand_restricted
                ):
                    stale_to_evict.append(cand_user.id)
                    continue

                if is_blocked:
                    continue

                cand_dto = _user_to_public_dto(cand_user)
                cand_prefs = preferences_model_to_dto(cand_user.id, cand_user.preferences)

                is_exact = is_mutual_match(
                    seeker_prefs,
                    seeker_dto,
                    cand_prefs,
                    cand_dto,
                    allow_relaxed=False,
                    now_ts=utc_now,
                )
                is_relaxed = False
                if not is_exact and (
                    seeker_prefs.has_relaxable_active_filters
                    or cand_prefs.has_relaxable_active_filters
                ):
                    is_relaxed = is_mutual_match(
                        seeker_prefs,
                        seeker_dto,
                        cand_prefs,
                        cand_dto,
                        allow_relaxed=True,
                        now_ts=utc_now,
                    )

                if not is_exact and not is_relaxed:
                    continue

                recent_tier = 1 if cand_user.id in recent_partner_ids else 0
                relaxed_tier = 0 if is_exact else 1
                joined_score = candidate_scores.get(cand_user.id, 0.0)
                scored_matches.append(
                    (relaxed_tier, recent_tier, joined_score, cand_user.id, not is_exact)
                )

            missing_ids = set(cand_ids) - found_ids
            stale_to_evict.extend(missing_ids)

        for stale_id in stale_to_evict:
            await self._queue_remove(stale_id)

        scored_matches.sort(key=lambda item: (item[0], item[1], item[2]))
        return [(cand_id, used_relaxed) for _, _, _, cand_id, used_relaxed in scored_matches]

    async def _try_create_random_session(
        self,
        *,
        seeker_id: int,
        partner_id: int,
        seeker_prefs: UserPreferencesDTO,
        used_relaxed: bool,
    ) -> MatchResult | None:
        """Atomically lock both users and create a RANDOM ChatSession in PostgreSQL."""
        utc_now = now_utc()
        first_id = min(seeker_id, partner_id)
        second_id = max(seeker_id, partner_id)

        try:
            async with self._db_manager.session() as session:
                users_res = await session.execute(
                    select(User)
                    .options(selectinload(User.profile), selectinload(User.preferences))
                    .where(User.id.in_([first_id, second_id]))
                    .order_by(User.id)
                    .with_for_update()
                )
                users_map = {u.id: u for u in users_res.scalars().all()}
                seeker = users_map.get(seeker_id)
                partner = users_map.get(partner_id)
                await reconcile_user_suspension_in_session(session, seeker, utc_now=utc_now)
                await reconcile_user_suspension_in_session(session, partner, utc_now=utc_now)

                if (
                    seeker is None
                    or partner is None
                    or seeker.status != "ACTIVE"
                    or partner.status != "ACTIVE"
                    or not seeker.age_18_plus_declared
                    or not partner.age_18_plus_declared
                    or seeker.profile is None
                    or partner.profile is None
                    or seeker.profile.completion_status != "COMPLETED"
                    or partner.profile.completion_status != "COMPLETED"
                ):
                    return None

                if await has_active_chat_restriction_in_session(
                    session, seeker.id, utc_now=utc_now
                ):
                    await self._queue_remove(seeker.id)
                    raise PermissionError("دسترسی گفتگوی شما توسط تیم نظارت محدود شده است.")

                if await has_active_chat_restriction_in_session(
                    session, partner.id, utc_now=utc_now
                ):
                    await self._queue_remove(partner.id)
                    return None

                if await has_bidirectional_block(session, seeker.id, partner.id):
                    return None

                active_rows = set(
                    (
                        await session.execute(
                            select(ActiveChatParticipant.user_id).where(
                                ActiveChatParticipant.user_id.in_([seeker.id, partner.id])
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if seeker.id in active_rows:
                    return MatchResult(status="ALREADY_IN_CHAT")
                if partner.id in active_rows:
                    await self._queue_remove(partner.id)
                    return None

                # Re-verify mutual compatibility under lock
                seeker_dto = _user_to_public_dto(seeker)
                partner_dto = _user_to_public_dto(partner)
                partner_prefs_obj = (
                    await session.execute(
                        select(UserPreferences).where(UserPreferences.user_id == partner.id)
                    )
                ).scalar_one_or_none()
                partner_prefs = preferences_model_to_dto(partner.id, partner_prefs_obj)

                if not is_mutual_match(
                    seeker_prefs,
                    seeker_dto,
                    partner_prefs,
                    partner_dto,
                    allow_relaxed=used_relaxed,
                    now_ts=utc_now,
                ):
                    return None

                chat_id = uuid.uuid4()
                chat_session = ChatSession(
                    id=chat_id,
                    chat_type="RANDOM",
                    direct_request_id=None,
                    direct_request_status=None,
                    participant_one_id=partner.id,
                    participant_two_id=seeker.id,
                    status="ACTIVE",
                    participant_one_sent_msg=False,
                    participant_two_sent_msg=False,
                    is_charged=False,
                    created_at=utc_now,
                    started_at=utc_now,
                )
                session.add(chat_session)
                await session.flush()

                session.add_all(
                    [
                        ActiveChatParticipant(
                            user_id=partner.id, chat_id=chat_id, joined_at=utc_now
                        ),
                        ActiveChatParticipant(
                            user_id=seeker.id, chat_id=chat_id, joined_at=utc_now
                        ),
                    ]
                )
                await ensure_bilateral_connections(session, seeker.id, partner.id, utc_now=utc_now)
                await session.flush()

                return MatchResult(
                    status="MATCHED",
                    chat_session_id=chat_id,
                    seeker_telegram_id=seeker.telegram_user_id,
                    partner_telegram_id=partner.telegram_user_id,
                    seeker_profile=seeker_dto,
                    partner_profile=partner_dto,
                    matched_via_relaxed=used_relaxed,
                )
        except IntegrityError:
            # Concurrency collision on ActiveChatParticipant PK: re-check seeker status
            async with self._db_manager.session() as session:
                seeker_active = (
                    await session.execute(
                        select(exists().where(ActiveChatParticipant.user_id == seeker_id))
                    )
                ).scalar_one()
                if seeker_active:
                    return MatchResult(status="ALREADY_IN_CHAT")
            return None

    async def _queue_add(self, user_id: int, timestamp: float) -> None:
        if self._redis_manager is not None:
            await self._redis_manager.client.zadd(
                self._queue_key, {str(user_id): timestamp}, nx=True
            )
        else:
            self._local_queue.setdefault(user_id, timestamp)

    async def _queue_remove(self, user_id: int) -> bool:
        if self._redis_manager is not None:
            removed = await self._redis_manager.client.zrem(self._queue_key, str(user_id))
            return int(removed) > 0
        return self._local_queue.pop(user_id, None) is not None

    async def _queue_contains(self, user_id: int) -> bool:
        if self._redis_manager is not None:
            score = await self._redis_manager.client.zscore(self._queue_key, str(user_id))
            return score is not None
        return user_id in self._local_queue

    async def _queue_get_all(self) -> list[tuple[int, float]]:
        if self._redis_manager is not None:
            raw = await self._redis_manager.client.zrange(self._queue_key, 0, -1, withscores=True)
            results: list[tuple[int, float]] = []
            for member, score in raw:
                try:
                    results.append((int(member), float(score)))
                except (ValueError, TypeError):
                    continue
            return results
        return sorted(self._local_queue.items(), key=lambda item: item[1])

    async def _prune_stale_queue_entries(self, min_score: float) -> None:
        if self._redis_manager is not None:
            await self._redis_manager.client.zremrangebyscore(self._queue_key, "-inf", min_score)
        else:
            stale = [uid for uid, score in self._local_queue.items() if score < min_score]
            for uid in stale:
                self._local_queue.pop(uid, None)
