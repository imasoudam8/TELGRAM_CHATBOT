"""Live PostgreSQL 16 + Redis 7 integration tests for TASK009 admin, moderation, and audit."""

import asyncio
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from scripts.bootstrap_admin import bootstrap_super_admin
from src.app.config.settings import Settings
from src.app.infrastructure.database.base import now_utc
from src.app.infrastructure.database.session import DatabaseManager
from src.app.infrastructure.redis.client import RedisManager
from src.app.modules.admin.config_service import (
    KEY_DAILY_FREE_COINS_QUOTA,
    KEY_DIRECT_CHAT_COST_COINS,
    KEY_DIRECT_REQUEST_EXPIRY_HOURS,
    KEY_MESSAGE_RETENTION_DAYS,
    KEY_RANDOM_CHAT_COST_COINS,
    KEY_VIDEO_SIZE_LIMIT_BYTES,
    AdminConfigService,
)
from src.app.modules.admin.models import AdminUser, SystemConfiguration
from src.app.modules.admin.rbac import ROLE_MODERATOR, ROLE_SUPER_ADMIN
from src.app.modules.admin.schemas import AdminConflictError
from src.app.modules.admin.service import AdminService
from src.app.modules.chat.direct_requests import DirectRequestService
from src.app.modules.chat.models import (
    ChatMessage,
    DirectRequest,
    MessageEvidenceHold,
    UserBlock,
)
from src.app.modules.chat.session_service import ChatService
from src.app.modules.discovery.service import DiscoveryService
from src.app.modules.matching.service import RandomMatchService
from src.app.modules.moderation.service import ReportService
from src.app.modules.profiles.service import ProfileData, ProfileService
from src.app.modules.users.models import User
from src.app.modules.users.service import UserService
from src.app.modules.wallet.schemas import InsufficientBalanceError
from src.app.modules.wallet.service import WalletService
from src.app.modules.wallet.settlement import ChatSettlementService
from tests.conftest import is_ci_environment, is_service_reachable


async def _register_user_with_profile(
    db: DatabaseManager,
    settings: Settings,
    tg_id: int,
    *,
    name: str,
    age: int,
    gender: str,
    city: str = "تهران",
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
            visibility="PUBLIC",
        ),
    )
    return user


@pytest.mark.asyncio
async def test_live_postgres_moderation_lifecycle_reporting_race_and_pair_block(
    migrated_postgres_db: tuple[str, Settings],
) -> None:
    """Verify PG16 RBAC, bootstrap, moderation enforcement/expiry, report holds, race & blocks."""
    _, settings = migrated_postgres_db
    db = DatabaseManager(settings)
    await db.initialize()

    redis: RedisManager | None = None
    if is_ci_environment() or is_service_reachable(settings.REDIS_HOST, settings.REDIS_PORT):
        redis = RedisManager(settings)
        await redis.initialize()

    queue_key = f"test:adm:queue:{uuid.uuid4().hex[:8]}"

    try:
        await _register_user_with_profile(
            db, settings, 960001, name="مدیر کل", age=30, gender="MALE"
        )
        mod1 = await _register_user_with_profile(
            db, settings, 960002, name="ناظر یک", age=28, gender="FEMALE"
        )
        mod2 = await _register_user_with_profile(
            db, settings, 960003, name="ناظر دو", age=29, gender="MALE"
        )
        alice = await _register_user_with_profile(
            db, settings, 960004, name="آلیس", age=24, gender="FEMALE"
        )
        bob = await _register_user_with_profile(
            db, settings, 960005, name="باب", age=25, gender="MALE"
        )
        charlie = await _register_user_with_profile(
            db, settings, 960006, name="چارلی", age=26, gender="MALE"
        )

        # Out-of-band bootstrap SUPER_ADMIN via script helper, then assign MODERATOR roles
        boot_res, boot_audit_id = await bootstrap_super_admin(
            db, telegram_user_id=960001, reason="PG16 bootstrap"
        )
        assert boot_res.role == ROLE_SUPER_ADMIN
        assert boot_audit_id > 0

        adm_svc = AdminService(db, settings)
        await adm_svc.assign_or_update_admin_role(
            960001, target_public_id=mod1.public_id, role=ROLE_MODERATOR, reason="Assign Mod 1"
        )
        await adm_svc.assign_or_update_admin_role(
            960001, target_public_id=mod2.public_id, role=ROLE_MODERATOR, reason="Assign Mod 2"
        )

        # 1. Start direct chat between Alice and Bob, send one message from Bob
        dr_svc = DirectRequestService(db)
        chat_svc = ChatService(db, settings)
        req_res = await dr_svc.send_direct_request(alice.id, bob.public_id)
        decision = await dr_svc.respond_to_direct_request(req_res.request_id, bob.id, accept=True)
        assert decision.chat_session_id is not None
        chat_id = decision.chat_session_id

        await chat_svc.prepare_and_record_message(
            sender_user_id=bob.id,
            message_type="TEXT",
            text_content="پیام نامناسب برای آزمایش گزارش",
        )

        # 2. Alice submits active chat report -> metadata-only MessageEvidenceHold created
        report_svc = ReportService(db)
        sub_rep = await report_svc.submit_active_chat_report(
            reporter_user_id=alice.id,
            chat_session_id=chat_id,
            category="HARASSMENT",
            description="آزار در گفتگو",
        )
        assert sub_rep.evidence_holds_created == 1

        rep_detail = await adm_svc.get_report_detail(960002, sub_rep.report_id)
        assert rep_detail.reported_public_id == bob.public_id
        assert len(rep_detail.evidence_holds) == 1
        assert rep_detail.evidence_holds[0].message_type == "TEXT"

        # Verify no plaintext leakage in Report/EvidenceHold records
        async with db.session() as session:
            hold_row = (
                await session.execute(
                    select(MessageEvidenceHold).where(
                        MessageEvidenceHold.report_id == sub_rep.report_id
                    )
                )
            ).scalar_one()
            assert "نامناسب" not in hold_row.reason
            assert len(hold_row.message_content_hash) == 64

        # 3. Concurrent report resolution race by 2 moderators -> exactly 1 succeeds, 1 conflicts
        race_results = await asyncio.gather(
            adm_svc.resolve_report(
                960002, sub_rep.report_id, new_status="REVIEWED", resolution_notes="Mod 1 review"
            ),
            adm_svc.resolve_report(
                960003, sub_rep.report_id, new_status="ACTIONED", resolution_notes="Mod 2 review"
            ),
            return_exceptions=True,
        )
        successes = [r for r in race_results if not isinstance(r, BaseException)]
        conflicts = [r for r in race_results if isinstance(r, AdminConflictError)]
        assert len(successes) == 1
        assert len(conflicts) == 1

        # 4. CHAT_RESTRICTION ends active chat and blocks chat, matching, and direct requests
        restr_outcome = await adm_svc.apply_moderation_action(
            960002,
            target_public_id=bob.public_id,
            action_type="CHAT_RESTRICTION",
            reason="Verified harassment",
            duration_hours=12,
        )
        assert restr_outcome.terminated_chat_id == chat_id
        assert restr_outcome.is_chat_restricted is True
        assert await chat_svc.get_active_chat_by_user_id(alice.id) is None

        # Non-chat profile view still works during CHAT_RESTRICTION; chat actions fail
        assert await ProfileService(db).get_profile_by_telegram_id(960005) is not None
        with pytest.raises(PermissionError):
            await dr_svc.send_direct_request(bob.id, charlie.public_id)
        with pytest.raises(LookupError):
            await dr_svc.send_direct_request(charlie.id, bob.public_id)
        if redis is not None:
            with pytest.raises(PermissionError):
                await RandomMatchService(db, redis, queue_key=queue_key).enter_queue_and_find_match(
                    bob.id
                )

        # Restore Bob's chat access manually via moderator
        restored = await adm_svc.restore_user_access(
            960002, target_public_id=bob.public_id, reason="Verified apology"
        )
        assert restored.is_chat_restricted is False

        # 5. Moderator pair block cancels pending request and prevents communication until unblocked
        req_bc = await dr_svc.send_direct_request(bob.id, charlie.public_id)
        blk_res = await adm_svc.admin_block_users(
            960002,
            blocker_public_id=charlie.public_id,
            blocked_public_id=bob.public_id,
            reason="Moderator separation",
        )
        assert blk_res.blocked is True

        async with db.session() as session:
            req_after = (
                await session.execute(
                    select(DirectRequest).where(DirectRequest.id == req_bc.request_id)
                )
            ).scalar_one()
            assert req_after.status == "CANCELLED"
            blk_row = (
                await session.execute(
                    select(UserBlock).where(
                        UserBlock.blocker_id == charlie.id,
                        UserBlock.blocked_id == bob.id,
                    )
                )
            ).scalar_one_or_none()
            assert blk_row is not None

        with pytest.raises(PermissionError):
            await dr_svc.send_direct_request(bob.id, charlie.public_id)

        unblk_res = await adm_svc.admin_unblock_users(
            960002,
            blocker_public_id=charlie.public_id,
            blocked_public_id=bob.public_id,
            reason="Separation lifted",
        )
        assert unblk_res.blocked is False

        # 6. TEMPORARY_SUSPENSION blocks profile/discovery/chat & auto-recovers after expiry
        if redis is not None:
            match_svc = RandomMatchService(db, redis, queue_key=queue_key)
            await match_svc.enter_queue_and_find_match(bob.id)

        await adm_svc.apply_moderation_action(
            960002,
            target_public_id=bob.public_id,
            action_type="TEMPORARY_SUSPENSION",
            reason="Temporary timeout",
            duration_hours=2,
        )
        if redis is not None:
            await RandomMatchService(db, redis, queue_key=queue_key).leave_queue(bob.id)

        with pytest.raises(PermissionError):
            await DiscoveryService(db).browse_profiles(960005)

        # Backdate suspended_until in PG to simulate elapsed suspension window -> automatic recovery
        async with db.session() as session:
            await session.execute(
                update(User)
                .where(User.id == bob.id)
                .values(suspended_until=now_utc() - timedelta(minutes=1))
            )

        recovered_bob = await UserService(db).get_user_by_telegram_id(960005)
        assert recovered_bob is not None
        assert recovered_bob.status == "ACTIVE"

        # 7. PERMANENT_BAN deactivates AdminUser if target was an active admin
        await adm_svc.apply_moderation_action(
            960001,
            target_public_id=mod2.public_id,
            action_type="PERMANENT_BAN",
            reason="Rogue moderator removal",
        )
        assert await adm_svc.get_active_admin_context(960003) is None
        async with db.session() as session:
            mod2_admin = (
                await session.execute(select(AdminUser).where(AdminUser.user_id == mod2.id))
            ).scalar_one()
            assert mod2_admin.is_active is False
    finally:
        if redis is not None:
            await redis.client.delete(queue_key)
            await redis.close()
        await db.close()


@pytest.mark.asyncio
async def test_live_postgres_wallet_admin_configs_immutability_and_atomic_rollback(
    migrated_postgres_db: tuple[str, Settings],
) -> None:
    """Verify wallet admin, dynamic config consumers, audit trigger, and full audit rollback."""
    _, settings = migrated_postgres_db
    db = DatabaseManager(settings)
    await db.initialize()

    try:
        await _register_user_with_profile(
            db, settings, 970001, name="مدیر کل", age=32, gender="MALE"
        )
        user_a = await _register_user_with_profile(
            db, settings, 970002, name="کاربر آ", age=25, gender="FEMALE"
        )
        user_b = await _register_user_with_profile(
            db, settings, 970003, name="کاربر ب", age=26, gender="MALE"
        )
        await bootstrap_super_admin(db, telegram_user_id=970001, reason="PG16 financial test")

        adm_svc = AdminService(db, settings)
        cfg_svc = AdminConfigService(db, settings)
        w_svc = WalletService(db, settings)

        # 1. SUPER_ADMIN wallet grant + idempotent replay + debit + insufficient balance rejection
        grant1 = await adm_svc.admin_grant_coins(
            970001,
            target_public_id=user_a.public_id,
            amount=10,
            reason="Support grant",
            idempotency_key="adm_grant_pg_1",
        )
        assert grant1.purchased_coins_after == 10
        assert grant1.audit_log_id is not None

        grant_replay = await adm_svc.admin_grant_coins(
            970001,
            target_public_id=user_a.public_id,
            amount=10,
            reason="Support grant",
            idempotency_key="adm_grant_pg_1",
        )
        assert grant_replay.already_applied is True
        assert grant_replay.purchased_coins_after == 10

        debit1 = await adm_svc.admin_debit_coins(
            970001,
            target_public_id=user_a.public_id,
            amount=4,
            reason="Chargeback adjustment",
            idempotency_key="adm_debit_pg_1",
        )
        assert debit1.purchased_coins_after == 6

        with pytest.raises(InsufficientBalanceError):
            await adm_svc.admin_debit_coins(
                970001,
                target_public_id=user_a.public_id,
                amount=999,
                reason="Too much",
                idempotency_key="adm_debit_pg_fail",
            )

        # 2. Atomic rollback guarantee: audit failure must roll back wallet & config mutations
        with patch(
            "src.app.modules.admin.service.record_audit_log_in_session",
            side_effect=RuntimeError("Simulated audit write failure"),
        ):
            with pytest.raises(RuntimeError, match="Simulated audit write failure"):
                await adm_svc.admin_grant_coins(
                    970001,
                    target_public_id=user_a.public_id,
                    amount=50,
                    reason="Should rollback",
                    idempotency_key="adm_grant_rollback_1",
                )

        bal_after_rollback = await w_svc.get_wallet_balance(user_a.id, auto_allocate_daily=False)
        assert bal_after_rollback.purchased_coins == 6

        with patch(
            "src.app.modules.admin.config_service.record_audit_log_in_session",
            side_effect=RuntimeError("Simulated config audit failure"),
        ):
            with pytest.raises(RuntimeError, match="Simulated config audit failure"):
                await cfg_svc.update_configuration(
                    970001,
                    key=KEY_DAILY_FREE_COINS_QUOTA,
                    value=12,
                    reason="Should rollback",
                )

        async with db.session() as session:
            rolled_cfg = (
                await session.execute(
                    select(SystemConfiguration).where(
                        SystemConfiguration.key == KEY_DAILY_FREE_COINS_QUOTA
                    )
                )
            ).scalar_one_or_none()
            assert rolled_cfg is None

        # 3. Dynamic SystemConfiguration changes drive runtime services without restart
        await cfg_svc.update_configuration(
            970001, key=KEY_DAILY_FREE_COINS_QUOTA, value=7, reason="Promo quota"
        )
        await cfg_svc.update_configuration(
            970001, key=KEY_RANDOM_CHAT_COST_COINS, value=3, reason="Tariff up"
        )
        await cfg_svc.update_configuration(
            970001, key=KEY_DIRECT_CHAT_COST_COINS, value=5, reason="Direct tariff up"
        )
        await cfg_svc.update_configuration(
            970001, key=KEY_DIRECT_REQUEST_EXPIRY_HOURS, value=48, reason="Longer request window"
        )
        await cfg_svc.update_configuration(
            970001, key=KEY_VIDEO_SIZE_LIMIT_BYTES, value=2_000_000, reason="2MB video limit"
        )
        await cfg_svc.update_configuration(
            970001, key=KEY_MESSAGE_RETENTION_DAYS, value=14, reason="14-day retention"
        )

        # Daily quota consumes updated value=7
        b_alloc = await w_svc.allocate_daily_allowance(user_b.id)
        assert b_alloc.free_coins == 7

        # DirectRequestService uses 48-hour expiry + settled cost=5 on accept & two-way msg
        dr_svc = DirectRequestService(db)
        chat_svc = ChatService(db, settings)
        before_req = now_utc()
        dr_res = await dr_svc.send_direct_request(user_a.id, user_b.public_id)
        async with db.session() as session:
            dr_row = (
                await session.execute(
                    select(DirectRequest).where(DirectRequest.id == dr_res.request_id)
                )
            ).scalar_one()
            delta_hours = (dr_row.expires_at - before_req).total_seconds() / 3600
            assert 47.5 <= delta_hours <= 48.5

        await dr_svc.respond_to_direct_request(dr_res.request_id, user_b.id, accept=True)

        # Video >= 2_000_000 rejected via updated SystemConfiguration limit
        with pytest.raises(ValueError, match="سقف مجاز"):
            await chat_svc.prepare_and_record_message(
                sender_user_id=user_a.id,
                message_type="VIDEO",
                telegram_file_id="vid_over_2mb",
                file_size_bytes=2_000_000,
            )

        # Two-way messages verify 14-day retention & direct_chat_cost_coins=5 charged to user_a
        msg_a = await chat_svc.prepare_and_record_message(
            sender_user_id=user_a.id, message_type="TEXT", text_content="سلام"
        )
        msg_b = await chat_svc.prepare_and_record_message(
            sender_user_id=user_b.id, message_type="TEXT", text_content="درود"
        )
        assert msg_b.both_participants_sent is True
        settle_res = await ChatSettlementService(db, settings).settle_chat(msg_b.chat_id)
        assert settle_res.status == "SETTLED"

        bal_a_settled = await w_svc.get_wallet_balance(user_a.id, auto_allocate_daily=False)
        # user_a: 6 purchased + 7 free = 13 -> paid 5 from free first -> 2 free, 6 purchased
        assert bal_a_settled.free_coins == 2
        assert bal_a_settled.purchased_coins == 6

        async with db.session() as session:
            db_msg = (
                await session.execute(select(ChatMessage).where(ChatMessage.id == msg_a.message_id))
            ).scalar_one()
            ret_days = (db_msg.expires_at - db_msg.created_at).total_seconds() / 86400
            assert 13.9 <= ret_days <= 14.1

        # 4. PostgreSQL hard check constraints reject > 10 MiB video limit & > 30 retention days
        with pytest.raises(IntegrityError):
            async with db.session() as session:
                await session.execute(
                    text(
                        "UPDATE system_configurations "
                        "SET value = '{\"limit\": 10485761}'::jsonb "
                        "WHERE key = 'video_size_limit_bytes'"
                    )
                )
        with pytest.raises(IntegrityError):
            async with db.session() as session:
                await session.execute(
                    text(
                        "UPDATE system_configurations "
                        "SET value = '{\"days\": 31}'::jsonb "
                        "WHERE key = 'message_retention_days'"
                    )
                )

        # 5. Append-only PostgreSQL trigger forbids UPDATE & DELETE on admin_audit_logs
        with pytest.raises(DBAPIError):
            async with db.session() as session:
                await session.execute(text("UPDATE admin_audit_logs SET reason = 'tampered'"))
        with pytest.raises(DBAPIError):
            async with db.session() as session:
                await session.execute(text("DELETE FROM admin_audit_logs"))

        # Confirm audit logs actually persisted cleanly
        audit_page = await adm_svc.list_audit_logs(970001, offset=0, limit=20)
        assert audit_page.total_count >= 9
    finally:
        await db.close()
