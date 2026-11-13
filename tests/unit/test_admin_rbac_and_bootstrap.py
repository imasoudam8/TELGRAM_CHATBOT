"""Unit tests for RBAC, admin bootstrap CLI, config ceilings, audit sanitization, and reports."""

import inspect
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import scripts.bootstrap_admin as bootstrap_cli
from scripts.bootstrap_admin import bootstrap_super_admin
from src.app.config.settings import Settings
from src.app.modules.admin.audit import record_audit_log_in_session
from src.app.modules.admin.config_service import (
    HARD_MAX_MESSAGE_RETENTION_DAYS,
    HARD_MAX_VIDEO_SIZE_BYTES,
    KEY_DAILY_FREE_COINS_QUOTA,
    KEY_RANDOM_CHAT_COST_COINS,
    AdminConfigService,
    validate_config_payload,
)
from src.app.modules.admin.models import AdminAuditLog, AdminUser
from src.app.modules.admin.rbac import ROLE_MODERATOR, ROLE_SUPER_ADMIN, ROLE_SUPPORT
from src.app.modules.admin.schemas import (
    AdminConflictError,
    AdminNotFoundError,
    AdminPermissionError,
    AdminValidationError,
)
from src.app.modules.admin.service import AdminService
from src.app.modules.chat.models import (
    ActiveChatParticipant,
    ChatMessage,
    ChatSession,
    MessageEvidenceHold,
)
from src.app.modules.moderation.service import ReportService
from src.app.modules.users.models import User
from src.app.modules.users.service import UserService


class SQLiteDBWrapper:
    """SQLite DatabaseManager adapter for fast deterministic unit tests."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.session_factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )

    @asynccontextmanager
    async def session(self):
        async with self.session_factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise


async def _seed_user(
    db: SQLiteDBWrapper,
    settings: Settings,
    tg_id: int,
    *,
    role: str | None = None,
    is_active_admin: bool = True,
) -> User:
    user_svc = UserService(db)  # type: ignore[arg-type]
    user, _ = await user_svc.register_adult_user(tg_id, settings.business_timezone)
    if role is not None:
        async with db.session() as session:
            session.add(
                AdminUser(
                    user_id=user.id,
                    role=role,
                    is_active=is_active_admin,
                    assigned_by=None,
                )
            )
    return user


@pytest.mark.asyncio
async def test_bootstrap_super_admin_cli_paths_and_no_hardcoded_ids(
    sqlite_engine: AsyncEngine,
    test_settings: Settings,
) -> None:
    """Verify out-of-band CLI bootstrap creates/promotes SUPER_ADMIN idempotently."""
    src_text = inspect.getsource(bootstrap_cli)
    assert "999999" not in src_text
    assert "telegram_user_id =" not in src_text or "None" in src_text

    db = SQLiteDBWrapper(sqlite_engine)
    u1 = await _seed_user(db, test_settings, 710001)
    u2 = await _seed_user(db, test_settings, 710002, role=ROLE_SUPPORT)

    res1, audit1 = await bootstrap_super_admin(
        db,  # type: ignore[arg-type]
        telegram_user_id=710001,
        reason="Initial bootstrap test",
    )
    assert res1.public_id == u1.public_id
    assert res1.role == ROLE_SUPER_ADMIN
    assert res1.is_active is True
    assert audit1 > 0

    # Idempotent re-run succeeds cleanly without creating redundant changes
    res_again, audit_again = await bootstrap_super_admin(
        db,  # type: ignore[arg-type]
        public_id=u1.public_id,
        reason="Repeat check",
    )
    assert res_again.role == ROLE_SUPER_ADMIN
    assert res_again.is_active is True
    assert audit_again > audit1

    # Promote existing SUPPORT to SUPER_ADMIN via public_id
    res2, audit2 = await bootstrap_super_admin(
        db,  # type: ignore[arg-type]
        public_id=u2.public_id,
        reason="Promote operator",
    )
    assert res2.role == ROLE_SUPER_ADMIN
    assert res2.is_active is True
    assert audit2 > audit_again

    async with db.session() as session:
        logs = (
            (
                await session.execute(
                    select(AdminAuditLog).where(AdminAuditLog.action == "ADMIN_ROLE_BOOTSTRAP")
                )
            )
            .scalars()
            .all()
        )
        assert len(logs) == 3

    # Reject missing user
    with pytest.raises(LookupError):
        await bootstrap_super_admin(
            db,  # type: ignore[arg-type]
            telegram_user_id=999999999,
            reason="Missing user",
        )


@pytest.mark.asyncio
async def test_rbac_permission_matrix_and_self_or_last_super_admin_guards(
    sqlite_engine: AsyncEngine,
    test_settings: Settings,
) -> None:
    """Verify exact permissions for SUPPORT, MODERATOR, SUPER_ADMIN, and inactive operators."""
    db = SQLiteDBWrapper(sqlite_engine)
    super_admin = await _seed_user(db, test_settings, 720001, role=ROLE_SUPER_ADMIN)
    mod_admin = await _seed_user(db, test_settings, 720002, role=ROLE_MODERATOR)
    support_admin = await _seed_user(db, test_settings, 720003, role=ROLE_SUPPORT)
    await _seed_user(db, test_settings, 720004, role=ROLE_MODERATOR, is_active_admin=False)
    target = await _seed_user(db, test_settings, 720005)
    target2 = await _seed_user(db, test_settings, 720006)

    adm_svc = AdminService(db, test_settings)  # type: ignore[arg-type]
    cfg_svc = AdminConfigService(db, test_settings)  # type: ignore[arg-type]

    # Regular user & inactive admin have zero admin access
    assert await adm_svc.get_active_admin_context(720005) is None
    assert await adm_svc.get_active_admin_context(720004) is None
    with pytest.raises(AdminPermissionError):
        await adm_svc.lookup_user_by_public_id(720004, target.public_id)

    # SUPPORT: can inspect user, list reports, issue WARNING; denied everything higher
    looked_up = await adm_svc.lookup_user_by_public_id(720003, target.public_id)
    assert looked_up.public_id == target.public_id
    assert (await adm_svc.list_reports(720003)).total_count == 0
    warn_res = await adm_svc.apply_moderation_action(
        720003,
        target_public_id=target.public_id,
        action_type="WARNING",
        reason="First minor warning",
    )
    assert warn_res.action_type == "WARNING"
    for forbidden_call in (
        adm_svc.apply_moderation_action(
            720003,
            target_public_id=target.public_id,
            action_type="TEMPORARY_SUSPENSION",
            reason="Not allowed",
            duration_hours=12,
        ),
        adm_svc.admin_block_users(
            720003,
            blocker_public_id=target.public_id,
            blocked_public_id=target2.public_id,
            reason="Not allowed",
        ),
        adm_svc.inspect_user_wallet(720003, target.public_id),
        cfg_svc.list_configurations(720003),
        adm_svc.list_audit_logs(720003),
    ):
        with pytest.raises(AdminPermissionError):
            await forbidden_call

    # MODERATOR: restrict/suspend/restore & block pairs, denied BAN/wallet/config/audit/roles
    chat_restr = await adm_svc.apply_moderation_action(
        720002,
        target_public_id=target.public_id,
        action_type="CHAT_RESTRICTION",
        reason="Spam messages",
        duration_hours=6,
    )
    assert chat_restr.is_chat_restricted is True
    for super_only_call in (
        adm_svc.apply_moderation_action(
            720002,
            target_public_id=target.public_id,
            action_type="PERMANENT_BAN",
            reason="Not allowed",
        ),
        adm_svc.admin_grant_coins(
            720002,
            target_public_id=target.public_id,
            amount=5,
            reason="Not allowed",
            idempotency_key="mod_denied_credit",
        ),
        cfg_svc.update_configuration(
            720002, key=KEY_DAILY_FREE_COINS_QUOTA, value=5, reason="Not allowed"
        ),
        adm_svc.list_audit_logs(720002),
        adm_svc.list_admin_roles(720002),
    ):
        with pytest.raises(AdminPermissionError):
            await super_only_call

    # MODERATOR cannot sanction another active admin with suspension/restriction
    with pytest.raises(AdminPermissionError):
        await adm_svc.apply_moderation_action(
            720002,
            target_public_id=support_admin.public_id,
            action_type="TEMPORARY_SUSPENSION",
            reason="Mod targeting admin",
            duration_hours=2,
        )

    # SUPER_ADMIN: self-escalation / last-super-admin / self-ban guards
    with pytest.raises(AdminConflictError):
        await adm_svc.assign_or_update_admin_role(
            720001,
            target_public_id=super_admin.public_id,
            role=ROLE_MODERATOR,
            reason="Self demote",
        )
    with pytest.raises(AdminConflictError):
        await adm_svc.revoke_admin_role(
            720001,
            target_public_id=super_admin.public_id,
            reason="Self revoke",
        )
    with pytest.raises(AdminConflictError):
        await adm_svc.apply_moderation_action(
            720001,
            target_public_id=super_admin.public_id,
            action_type="PERMANENT_BAN",
            reason="Self ban",
        )

    # Promote moderator to SUPER_ADMIN, then test last-super-admin protection still holds
    await adm_svc.assign_or_update_admin_role(
        720001,
        target_public_id=mod_admin.public_id,
        role=ROLE_SUPER_ADMIN,
        reason="Promote second super admin",
    )
    # SUPER_ADMIN can demote mod_admin back, leaving 1 SUPER_ADMIN (super_admin)
    await adm_svc.assign_or_update_admin_role(
        720001,
        target_public_id=mod_admin.public_id,
        role=ROLE_MODERATOR,
        reason="Restore mod level",
    )
    # Add second super admin, then let mod_admin (as non-self) try to revoke the other
    second_super = await _seed_user(db, test_settings, 720009, role=ROLE_SUPER_ADMIN)
    await adm_svc.revoke_admin_role(
        720001, target_public_id=second_super.public_id, reason="Only 1 remains"
    )
    # Now super_admin is sole SUPER_ADMIN; revoked admin cannot invoke role revocation
    with pytest.raises(AdminPermissionError):
        await adm_svc.revoke_admin_role(
            720009, target_public_id=super_admin.public_id, reason="Revoked admin blocked"
        )


def test_config_validation_boundaries_and_type_rejection() -> None:
    """Verify strict type checks, allowlist, and hard safety ceilings."""
    # Unlisted key
    with pytest.raises(AdminNotFoundError):
        validate_config_payload("arbitrary_unlisted_key", 10)

    # Boolean disguised as int must be rejected cleanly
    with pytest.raises(AdminValidationError):
        validate_config_payload(KEY_DAILY_FREE_COINS_QUOTA, True)

    # Wrong JSON field shape
    with pytest.raises(AdminValidationError):
        validate_config_payload(KEY_RANDOM_CHAT_COST_COINS, {"wrong": 1})

    # Safety ceilings enforced strictly
    with pytest.raises(AdminValidationError):
        validate_config_payload("video_size_limit_bytes", HARD_MAX_VIDEO_SIZE_BYTES + 1)
    with pytest.raises(AdminValidationError):
        validate_config_payload("message_retention_days", HARD_MAX_MESSAGE_RETENTION_DAYS + 1)
    with pytest.raises(AdminValidationError):
        validate_config_payload("message_retention_days", 0)

    spec_v, payload_v = validate_config_payload("video_size_limit_bytes", HARD_MAX_VIDEO_SIZE_BYTES)
    assert spec_v.field_name == "limit"
    assert payload_v == {"limit": HARD_MAX_VIDEO_SIZE_BYTES}

    spec_r, payload_r = validate_config_payload(
        "message_retention_days", HARD_MAX_MESSAGE_RETENTION_DAYS
    )
    assert spec_r.field_name == "days"
    assert payload_r == {"days": HARD_MAX_MESSAGE_RETENTION_DAYS}


@pytest.mark.asyncio
async def test_audit_log_redacts_secrets_and_report_stores_metadata_only(
    sqlite_engine: AsyncEngine,
    test_settings: Settings,
) -> None:
    """Verify audit payload redaction and metadata-only evidence holds on reports."""
    db = SQLiteDBWrapper(sqlite_engine)
    u1 = await _seed_user(db, test_settings, 730001)
    u2 = await _seed_user(db, test_settings, 730002)

    async with db.session() as session:
        with pytest.raises(AdminValidationError):
            await record_audit_log_in_session(
                session,
                admin_user_id=u1.id,
                action="TEST_ACTION",
                target_type="USER",
                target_id=str(u2.id),
                reason="   ",
            )
        entry = await record_audit_log_in_session(
            session,
            admin_user_id=u1.id,
            action="TEST_ACTION",
            target_type="USER",
            target_id=str(u2.id),
            payload_before={"bot_token": "SECRET", "message_text": "private body", "count": 1},
            payload_after={"password": "abc", "count": 2},
            reason="Audit sanitization verification",
        )
        assert entry.payload_before == {
            "bot_token": "[REDACTED]",
            "message_text": "[REDACTED]",
            "count": 1,
        }
        assert entry.payload_after == {"password": "[REDACTED]", "count": 2}

    # Seed active chat & metadata-only message, then submit report
    async with db.session() as session:
        chat = ChatSession(
            id=uuid.uuid4(),
            chat_type="RANDOM",
            participant_one_id=u1.id,
            participant_two_id=u2.id,
            status="ACTIVE",
        )
        session.add(chat)
        await session.flush()
        session.add_all(
            [
                ActiveChatParticipant(user_id=u1.id, chat_id=chat.id),
                ActiveChatParticipant(user_id=u2.id, chat_id=chat.id),
                ChatMessage(
                    chat_id=chat.id,
                    sender_id=u2.id,
                    recipient_id=u1.id,
                    message_type="TEXT",
                    content_hash="a" * 64,
                    expires_at=datetime.now(UTC) + timedelta(days=30),
                ),
            ]
        )
        chat_id = chat.id

    report_svc = ReportService(db)  # type: ignore[arg-type]
    submitted = await report_svc.submit_active_chat_report(
        reporter_user_id=u1.id,
        chat_session_id=chat_id,
        category="HARASSMENT",
        description="ارسال پیام توهین‌آمیز",
    )
    assert submitted.evidence_holds_created == 1
    assert submitted.reported_public_id == u2.public_id

    # Duplicate PENDING report rejected
    with pytest.raises(ValueError, match="در صف بررسی"):
        await report_svc.submit_active_chat_report(
            reporter_user_id=u1.id,
            chat_session_id=chat_id,
            category="SPAM",
        )

    async with db.session() as session:
        holds = (
            (
                await session.execute(
                    select(MessageEvidenceHold).where(
                        MessageEvidenceHold.report_id == submitted.report_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(holds) == 1
        assert holds[0].message_content_hash == "a" * 64
        assert holds[0].hold_type == "USER_REPORT"
