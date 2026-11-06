"""Unit tests for SQLAlchemy 2.x ORM models, metadata registry, and entity constraints."""

import uuid
from datetime import date, datetime, timedelta, timezone

try:
    from datetime import UTC
except ImportError:
    UTC = timezone.utc  # noqa: UP017

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.app.infrastructure.database.base import Base
from src.app.infrastructure.database.models import (
    ActiveChatParticipant,
    AdminAuditLog,
    AdminUser,
    ChatMessage,
    ChatSession,
    DirectRequest,
    MessageEvidenceHold,
    ModerationAction,
    Report,
    SystemConfiguration,
    User,
    UserBlock,
    UserConnection,
    UserPreferences,
    UserProfile,
    Wallet,
    WalletDailyAllowance,
    WalletLedger,
)


def test_metadata_registry_contains_all_entities() -> None:
    """Verify that all 18 specified domain entities are correctly registered in Base.metadata."""
    table_names = set(Base.metadata.tables.keys())
    expected_tables = {
        "users",
        "user_profiles",
        "user_preferences",
        "direct_requests",
        "user_connections",
        "user_blocks",
        "chat_sessions",
        "active_chat_participants",
        "chat_messages",
        "message_evidence_holds",
        "wallets",
        "wallet_daily_allowances",
        "wallet_ledgers",
        "system_configurations",
        "reports",
        "moderation_actions",
        "admin_users",
        "admin_audit_logs",
    }
    assert expected_tables.issubset(table_names), f"Missing tables: {expected_tables - table_names}"


def test_model_mapper_configurations() -> None:
    """Verify ORM mapper inspection and primary key configurations across all entities."""
    mappers = [
        inspect(User),
        inspect(UserProfile),
        inspect(UserPreferences),
        inspect(DirectRequest),
        inspect(UserConnection),
        inspect(UserBlock),
        inspect(ChatSession),
        inspect(ActiveChatParticipant),
        inspect(ChatMessage),
        inspect(MessageEvidenceHold),
        inspect(Wallet),
        inspect(WalletDailyAllowance),
        inspect(WalletLedger),
        inspect(SystemConfiguration),
        inspect(Report),
        inspect(ModerationAction),
        inspect(AdminUser),
        inspect(AdminAuditLog),
    ]
    for mapper in mappers:
        assert mapper.primary_key, f"Mapper for {mapper.class_.__name__} has no primary key defined"


@pytest.mark.asyncio
async def test_user_and_profile_relationship(sqlite_session: AsyncSession) -> None:
    """Verify user registration, age declaration, and profile relationship on SQLite."""
    now_utc = datetime.now(UTC)
    today = date.today()

    user = User(
        telegram_user_id=100001,
        public_id="usr_test100001",
        status="ACTIVE",
        age_18_plus_declared=True,
        age_declared_at=now_utc,
        age_declared_date=today,
    )
    sqlite_session.add(user)
    await sqlite_session.flush()

    assert user.id is not None

    profile = UserProfile(
        user_id=user.id,
        age_18_plus_declared=True,
        age_declared_date=today,
        birth_date=today - timedelta(days=365 * 25),
        display_name="Test User",
        gender="MALE",
        city="Tehran",
        completion_status="COMPLETED",
        visibility="PUBLIC",
    )
    sqlite_session.add(profile)
    await sqlite_session.flush()

    assert profile.user_id == user.id

    stmt = select(User).options(selectinload(User.profile)).where(User.id == user.id)
    res = await sqlite_session.execute(stmt)
    fetched_user = res.scalar_one()

    assert fetched_user.profile is not None
    assert fetched_user.profile.display_name == "Test User"


@pytest.mark.asyncio
async def test_direct_request_and_chat_session_linkage(sqlite_session: AsyncSession) -> None:
    """Verify direct request creation, acceptance, and direct chat session linkage."""
    now = datetime.now(UTC)
    today = date.today()

    u1 = User(
        telegram_user_id=200001,
        public_id="usr_req2001",
        status="ACTIVE",
        age_18_plus_declared=True,
        age_declared_at=now,
        age_declared_date=today,
    )
    u2 = User(
        telegram_user_id=200002,
        public_id="usr_tgt2002",
        status="ACTIVE",
        age_18_plus_declared=True,
        age_declared_at=now,
        age_declared_date=today,
    )
    sqlite_session.add_all([u1, u2])
    await sqlite_session.flush()

    dreq = DirectRequest(
        requester_id=u1.id,
        target_id=u2.id,
        status="ACCEPTED",
        expires_at=now + timedelta(hours=24),
    )
    sqlite_session.add(dreq)
    await sqlite_session.flush()

    chat = ChatSession(
        id=uuid.uuid4(),
        chat_type="DIRECT",
        direct_request_id=dreq.id,
        direct_request_status="ACCEPTED",
        participant_one_id=u1.id,
        participant_two_id=u2.id,
        status="INITIATED",
    )
    sqlite_session.add(chat)
    await sqlite_session.flush()

    assert chat.chat_type == "DIRECT"
    assert chat.direct_request_id == dreq.id
    assert chat.participant_one_id == u1.id
    assert chat.participant_two_id == u2.id


@pytest.mark.asyncio
async def test_active_chat_participant_uniqueness(sqlite_session: AsyncSession) -> None:
    """Verify single active chat participant junction entity mapping."""
    now = datetime.now(UTC)
    today = date.today()

    u = User(
        telegram_user_id=300001,
        public_id="usr_act3001",
        status="ACTIVE",
        age_18_plus_declared=True,
        age_declared_at=now,
        age_declared_date=today,
    )
    sqlite_session.add(u)
    await sqlite_session.flush()

    chat_id = uuid.uuid4()
    participant = ActiveChatParticipant(
        user_id=u.id,
        chat_id=chat_id,
        joined_at=now,
    )
    sqlite_session.add(participant)
    await sqlite_session.flush()

    assert participant.user_id == u.id
    assert participant.chat_id == chat_id


@pytest.mark.asyncio
async def test_wallet_and_ledger_append_only_mapping(sqlite_session: AsyncSession) -> None:
    """Verify wallet creation and ledger journal record insertion."""
    now = datetime.now(UTC)
    today = date.today()

    u = User(
        telegram_user_id=400001,
        public_id="usr_wal4001",
        status="ACTIVE",
        age_18_plus_declared=True,
        age_declared_at=now,
        age_declared_date=today,
    )
    sqlite_session.add(u)
    await sqlite_session.flush()

    wallet = Wallet(user_id=u.id, purchased_coins=10)
    sqlite_session.add(wallet)

    ledger_entry = WalletLedger(
        wallet_user_id=u.id,
        idempotency_key="tx_test_grant_001",
        transaction_type="ADMIN_GRANT",
        credit_type="PURCHASED",
        amount=10,
        purchased_balance_before=0,
        purchased_balance_after=10,
        free_balance_before=0,
        free_balance_after=0,
        actor_type="ADMIN",
        reason="Initial test grant",
    )
    sqlite_session.add(ledger_entry)
    await sqlite_session.flush()

    assert wallet.purchased_coins == 10
    assert ledger_entry.idempotency_key == "tx_test_grant_001"
