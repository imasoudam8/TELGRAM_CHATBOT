"""Live PostgreSQL 16 + Redis 7 integration tests for TASK007 workflows."""

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from src.app.config.settings import Settings
from src.app.infrastructure.database.base import now_utc
from src.app.infrastructure.database.session import DatabaseManager
from src.app.infrastructure.redis.client import RedisManager
from src.app.modules.chat.connections import ConnectionAndBlockService
from src.app.modules.chat.direct_requests import DirectRequestService
from src.app.modules.chat.models import (
    ActiveChatParticipant,
    ChatMessage,
    ChatSession,
    DirectRequest,
)
from src.app.modules.chat.session_service import VIDEO_SIZE_LIMIT_BYTES, ChatService
from src.app.modules.discovery.service import DiscoveryService
from src.app.modules.matching.service import RandomMatchService
from src.app.modules.profiles.service import ProfileData, ProfileService
from src.app.modules.users.models import User, UserProfile
from src.app.modules.users.service import UserService


async def _create_completed_user(
    db: DatabaseManager,
    settings: Settings,
    tg_id: int,
    *,
    name: str,
    age: int,
    gender: str,
    city: str | None = None,
    bio: str | None = None,
    visibility: str = "PUBLIC",
    status: str = "ACTIVE",
) -> User:
    user_svc = UserService(db)
    prof_svc = ProfileService(db)
    user, _ = await user_svc.register_adult_user(tg_id, settings.business_timezone)
    await prof_svc.create_profile(
        tg_id,
        ProfileData(
            display_name=name,
            age=age,
            gender=gender,
            city=city,
            bio=bio,
            visibility=visibility,
        ),
    )
    if status != "ACTIVE":
        async with db.session() as session:
            db_u = (await session.execute(select(User).where(User.id == user.id))).scalar_one()
            db_u.status = status
    return user


@pytest.mark.asyncio
async def test_live_discovery_filtering_pagination_and_public_id_lookup(
    migrated_postgres_db: tuple[str, Settings],
) -> None:
    """Verify discovery eligibility, strict vs relaxable filters, pagination, and lookup."""
    _, settings = migrated_postgres_db
    db = DatabaseManager(settings)
    await db.initialize()

    try:
        viewer = await _create_completed_user(
            db, settings, 810001, name="بیننده", age=26, gender="MALE", city="تهران"
        )
        match_exact_1 = await _create_completed_user(
            db, settings, 810002, name="مخاطب یک", age=24, gender="FEMALE", city="تهران"
        )
        match_exact_2 = await _create_completed_user(
            db, settings, 810003, name="مخاطب دو", age=27, gender="FEMALE", city="تهران"
        )
        _shiraz_female = await _create_completed_user(
            db, settings, 810004, name="شیرازی", age=25, gender="FEMALE", city="شیراز"
        )
        hidden_user = await _create_completed_user(
            db,
            settings,
            810005,
            name="مخفی",
            age=25,
            gender="FEMALE",
            city="تهران",
            visibility="HIDDEN",
        )
        suspended_user = await _create_completed_user(
            db,
            settings,
            810006,
            name="مسدود سیستمی",
            age=25,
            gender="FEMALE",
            city="تهران",
            status="SUSPENDED",
        )
        blocked_peer = await _create_completed_user(
            db, settings, 810007, name="بلاک شده", age=25, gender="FEMALE", city="تهران"
        )
        # Incomplete profile user
        incomp_user, _ = await UserService(db).register_adult_user(
            810008, settings.business_timezone
        )
        async with db.session() as session:
            session.add(
                UserProfile(
                    user_id=incomp_user.id,
                    age_18_plus_declared=True,
                    age_declared_date=incomp_user.age_declared_date,
                    age=25,
                    display_name="ناقص",
                    gender="FEMALE",
                    city="تهران",
                    completion_status="INCOMPLETE",
                    visibility="PUBLIC",
                )
            )

        # Viewer blocks blocked_peer
        blk_svc = ConnectionAndBlockService(db)
        await blk_svc.block_user(viewer.id, blocked_peer.public_id)

        # Configure strict preferences: FEMALE, 22-30, Tehran, NO relaxable fields
        disc_svc = DiscoveryService(db)
        await disc_svc.save_preferences(
            viewer.id,
            preferred_genders=["FEMALE"],
            min_age=22,
            max_age=30,
            preferred_cities=["تهران"],
            relaxable_fields=set(),
        )

        # Page 1
        page0 = await disc_svc.browse_profiles(viewer.id, offset=0, limit=1, use_relaxed=False)
        assert page0.total_count == 2
        assert len(page0.items) == 1
        assert page0.has_next is True
        assert page0.has_prev is False
        assert page0.relaxed_available is False  # Relaxable not permitted!

        # Page 2
        page1 = await disc_svc.browse_profiles(viewer.id, offset=1, limit=1, use_relaxed=False)
        assert len(page1.items) == 1
        assert page1.has_next is False
        assert page1.has_prev is True
        seen_pids = {page0.items[0].public_id, page1.items[0].public_id}
        assert seen_pids == {match_exact_1.public_id, match_exact_2.public_id}

        # Strict preference never silently relaxed even if 0 exact results
        await disc_svc.save_preferences(
            viewer.id,
            preferred_genders=["FEMALE"],
            min_age=22,
            max_age=30,
            preferred_cities=["مشهد"],
            relaxable_fields=set(),
        )
        empty_strict = await disc_svc.browse_profiles(
            viewer.id, offset=0, limit=1, use_relaxed=False
        )
        assert empty_strict.total_count == 0
        assert empty_strict.relaxed_available is False

        # Enable relaxable city explicitly -> proposals available, not returned until opted in
        await disc_svc.save_preferences(
            viewer.id,
            preferred_genders=["FEMALE"],
            min_age=22,
            max_age=30,
            preferred_cities=["مشهد"],
            relaxable_fields={"city"},
        )
        empty_with_offer = await disc_svc.browse_profiles(
            viewer.id, offset=0, limit=1, use_relaxed=False
        )
        assert empty_with_offer.total_count == 0
        assert empty_with_offer.relaxed_available is True
        assert empty_with_offer.relaxed_count == 3  # match_exact_1, match_exact_2, shiraz_female

        # Explicit relaxed browse respects strict gender/age while relaxing city
        relaxed_page = await disc_svc.browse_profiles(
            viewer.id, offset=0, limit=5, use_relaxed=True
        )
        assert relaxed_page.used_relaxed is True
        assert relaxed_page.total_count == 3

        # Public ID lookup enforces visibility, status, self, and blocks
        assert (
            await disc_svc.get_public_profile_by_public_id(viewer.id, match_exact_1.public_id)
            is not None
        )
        assert await disc_svc.get_public_profile_by_public_id(viewer.id, viewer.public_id) is None
        assert (
            await disc_svc.get_public_profile_by_public_id(viewer.id, hidden_user.public_id) is None
        )
        assert (
            await disc_svc.get_public_profile_by_public_id(viewer.id, suspended_user.public_id)
            is None
        )
        assert (
            await disc_svc.get_public_profile_by_public_id(viewer.id, blocked_peer.public_id)
            is None
        )
        assert (
            await disc_svc.get_public_profile_by_public_id(viewer.id, incomp_user.public_id) is None
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_live_direct_requests_expiry_concurrency_and_chat_lifecycle(
    migrated_postgres_db: tuple[str, Settings],
) -> None:
    """Verify direct request rules, 24h expiry, accept race, chat relay, end, and blocks."""
    _, settings = migrated_postgres_db
    db = DatabaseManager(settings)
    await db.initialize()

    try:
        alice = await _create_completed_user(
            db, settings, 820001, name="آلیس", age=24, gender="FEMALE", city="تهران"
        )
        bob = await _create_completed_user(
            db, settings, 820002, name="باب", age=28, gender="MALE", city="تهران"
        )
        charlie = await _create_completed_user(
            db, settings, 820003, name="چارلی", age=29, gender="MALE", city="تهران"
        )

        dr_svc = DirectRequestService(db)
        chat_svc = ChatService(db)
        conn_svc = ConnectionAndBlockService(db)

        # Self request rejected
        with pytest.raises(ValueError):
            await dr_svc.send_direct_request(alice.id, alice.public_id)

        # Send direct request Alice -> Bob
        sent = await dr_svc.send_direct_request(alice.id, bob.public_id)
        assert sent.request_id > 0

        # Duplicate pending in same direction rejected
        with pytest.raises(ValueError):
            await dr_svc.send_direct_request(alice.id, bob.public_id)

        # Unauthorized actor (Charlie) cannot accept/reject Bob's request
        with pytest.raises(PermissionError):
            await dr_svc.respond_to_direct_request(sent.request_id, charlie.id, accept=True)

        # Concurrent accept race on the same request: only 1 succeeds
        r1, r2 = await asyncio.gather(
            dr_svc.respond_to_direct_request(sent.request_id, bob.id, accept=True),
            dr_svc.respond_to_direct_request(sent.request_id, bob.id, accept=True),
            return_exceptions=True,
        )
        successes = [r for r in (r1, r2) if not isinstance(r, Exception)]
        failures = [r for r in (r1, r2) if isinstance(r, Exception)]
        assert len(successes) == 1
        assert len(failures) == 1
        decision = successes[0]
        assert decision.chat_session_id is not None

        # Verify DIRECT ChatSession + ActiveChatParticipant + Wallet boundary
        async with db.session() as session:
            cs = (
                await session.execute(
                    select(ChatSession).where(ChatSession.id == decision.chat_session_id)
                )
            ).scalar_one()
            assert cs.chat_type == "DIRECT"
            assert cs.direct_request_id == sent.request_id
            assert cs.direct_request_status == "ACCEPTED"
            assert cs.participant_one_id == alice.id
            assert cs.participant_two_id == bob.id
            assert cs.is_charged is False
            assert cs.charged_at is None

            wallet_count = (
                await session.execute(text("SELECT COUNT(*) FROM wallets"))
            ).scalar_one()
            ledger_count = (
                await session.execute(text("SELECT COUNT(*) FROM wallet_ledgers"))
            ).scalar_one()
            assert wallet_count == 0
            assert ledger_count == 0

        # Relay messages from both sides + verify 30-day retention & PostgreSQL constraints
        await chat_svc.prepare_and_record_message(
            sender_user_id=alice.id, message_type="TEXT", text_content="سلام باب!"
        )
        await chat_svc.prepare_and_record_message(
            sender_user_id=bob.id,
            message_type="VIDEO",
            telegram_file_id="vid_ok_1",
            file_size_bytes=5_000_000,
            duration_seconds=12,
        )
        with pytest.raises(ValueError):
            await chat_svc.prepare_and_record_message(
                sender_user_id=bob.id,
                message_type="VIDEO",
                telegram_file_id="vid_too_big",
                file_size_bytes=VIDEO_SIZE_LIMIT_BYTES,
            )

        # DB constraint also enforces video size ceiling at SQL level
        with pytest.raises(IntegrityError):
            async with db.session() as session:
                ts = now_utc()
                session.add(
                    ChatMessage(
                        chat_id=decision.chat_session_id,
                        sender_id=bob.id,
                        recipient_id=alice.id,
                        message_type="VIDEO",
                        telegram_file_id="vid_sql_reject",
                        file_size_bytes=VIDEO_SIZE_LIMIT_BYTES,
                        created_at=ts,
                        expires_at=ts + timedelta(days=30),
                    )
                )
                await session.flush()

        # Verify expires_at <= created_at + 30 days & sent flags updated & is_charged still False
        async with db.session() as session:
            cs_after = (
                await session.execute(
                    select(ChatSession).where(ChatSession.id == decision.chat_session_id)
                )
            ).scalar_one()
            assert cs_after.participant_one_sent_msg is True
            assert cs_after.participant_two_sent_msg is True
            assert cs_after.is_charged is False

            msgs = (
                (
                    await session.execute(
                        select(ChatMessage).where(ChatMessage.chat_id == decision.chat_session_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(msgs) == 2
            for m in msgs:
                assert m.expires_at <= m.created_at + timedelta(days=30)

        # End chat atomically clears both ActiveChatParticipant rows
        ended = await chat_svc.end_chat(alice.id, reason="USER_LEFT")
        assert ended is not None
        assert await chat_svc.get_active_chat_by_user_id(alice.id) is None
        assert await chat_svc.get_active_chat_by_user_id(bob.id) is None

        # Connections: asymmetric removal removes Alice -> Bob ONLY, leaves Bob -> Alice ACTIVE
        alice_conns = await conn_svc.list_connections(alice.id)
        bob_conns = await conn_svc.list_connections(bob.id)
        assert [c.public_id for c in alice_conns] == [bob.public_id]
        assert [c.public_id for c in bob_conns] == [alice.public_id]

        assert await conn_svc.remove_connection(alice.id, bob.public_id) is True
        assert await conn_svc.list_connections(alice.id) == []
        bob_conns_after = await conn_svc.list_connections(bob.id)
        assert [c.public_id for c in bob_conns_after] == [alice.public_id]

        # Expired request cannot be accepted
        exp_req = DirectRequest(
            requester_id=charlie.id,
            target_id=alice.id,
            status="PENDING",
            expires_at=now_utc() - timedelta(minutes=5),
        )
        async with db.session() as session:
            session.add(exp_req)
            await session.flush()
            exp_id = exp_req.id

        with pytest.raises(RuntimeError, match="مهلت"):
            await dr_svc.respond_to_direct_request(exp_id, alice.id, accept=True)

        async with db.session() as session:
            refetched_exp = (
                await session.execute(select(DirectRequest).where(DirectRequest.id == exp_id))
            ).scalar_one()
            assert refetched_exp.status == "EXPIRED"

        # Blocking between Charlie and Alice prevents direct requests in both directions
        await conn_svc.block_user(alice.id, charlie.public_id)
        with pytest.raises(PermissionError):
            await dr_svc.send_direct_request(charlie.id, alice.public_id)
        with pytest.raises(PermissionError):
            await dr_svc.send_direct_request(alice.id, charlie.public_id)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_live_random_matching_redis_concurrency_block_and_recent_avoidance(
    migrated_postgres_db: tuple[str, Settings],
) -> None:
    """Verify random matching with PostgreSQL 16 + Redis 7 under concurrency and avoidance."""
    _, settings = migrated_postgres_db
    db = DatabaseManager(settings)
    redis = RedisManager(settings)
    await db.initialize()
    await redis.initialize()

    test_queue_key = f"test:matching:queue:{uuid.uuid4().hex[:8]}"

    try:
        u1 = await _create_completed_user(
            db, settings, 830001, name="کاربر ۱", age=25, gender="MALE", city="تهران"
        )
        u2 = await _create_completed_user(
            db, settings, 830002, name="کاربر ۲", age=24, gender="FEMALE", city="تهران"
        )
        u3 = await _create_completed_user(
            db, settings, 830003, name="کاربر ۳", age=26, gender="FEMALE", city="تهران"
        )
        disc_svc = DiscoveryService(db)
        await disc_svc.save_preferences(u1.id, preferred_genders=["FEMALE"])
        await disc_svc.save_preferences(u2.id, preferred_genders=["MALE"])
        await disc_svc.save_preferences(u3.id, preferred_genders=["MALE"])

        # Incomplete user cannot enter random matching
        incomp, _ = await UserService(db).register_adult_user(830004, settings.business_timezone)
        matcher = RandomMatchService(db, redis, queue_key=test_queue_key)
        with pytest.raises(PermissionError):
            await matcher.enter_queue_and_find_match(incomp.id)

        # u1 enters queue first -> waits
        res_u1 = await matcher.enter_queue_and_find_match(u1.id)
        assert res_u1.status == "QUEUED"
        assert await matcher.is_in_queue(u1.id) is True

        # u1 can leave cleanly
        assert await matcher.leave_queue(u1.id) is True
        assert await matcher.is_in_queue(u1.id) is False

        # Re-enter u1 and concurrently attempt u2 & u3 entering at the exact same instant
        res_u1_re = await matcher.enter_queue_and_find_match(u1.id)
        assert res_u1_re.status == "QUEUED"

        m2, m3 = await asyncio.gather(
            matcher.enter_queue_and_find_match(u2.id),
            matcher.enter_queue_and_find_match(u3.id),
        )
        statuses = sorted([m2.status, m3.status])
        assert statuses == ["MATCHED", "QUEUED"]

        matched_res = m2 if m2.status == "MATCHED" else m3
        queued_user_id = u3.id if m2.status == "MATCHED" else u2.id
        first_partner_id = u2.id if m2.status == "MATCHED" else u3.id

        # End u1's chat -> queued_user_id is waiting and fresher than first_partner_id
        chat_svc = ChatService(db)
        await chat_svc.end_chat(u1.id, reason="USER_LEFT")

        # Put first_partner_id back AFTER queued_user_id is already waiting,
        # and verify u1 matches fresh partner (queued_user_id) rather than immediate recent partner
        await matcher.enter_queue_and_find_match(first_partner_id)
        fresh_match = await matcher.enter_queue_and_find_match(u1.id)
        assert fresh_match.status == "MATCHED"
        assert fresh_match.partner_profile is not None
        assert fresh_match.partner_profile.user_id == queued_user_id

        # Blocking during active random chat immediately ends chat with end_reason='BLOCKED'
        blk_svc = ConnectionAndBlockService(db)
        blk_res = await blk_svc.block_active_chat_partner(u1.id, fresh_match.chat_session_id)
        assert blk_res.terminated_chat_id == fresh_match.chat_session_id

        async with db.session() as session:
            cs_blk = (
                await session.execute(
                    select(ChatSession).where(ChatSession.id == fresh_match.chat_session_id)
                )
            ).scalar_one()
            assert cs_blk.status == "ENDED"
            assert cs_blk.end_reason == "BLOCKED"
            remaining_active = (
                (
                    await session.execute(
                        select(ActiveChatParticipant).where(
                            ActiveChatParticipant.chat_id == fresh_match.chat_session_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert remaining_active == []
            assert matched_res.chat_session_id != fresh_match.chat_session_id
    finally:
        try:
            await redis.client.delete(test_queue_key)
        except Exception:
            pass
        await redis.close()
        await db.close()
