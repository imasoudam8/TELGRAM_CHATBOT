"""Unit tests for TASK009 Telegram admin/moderation handlers, RBAC UI, and replay guards."""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.app.bot.handlers.admin import (
    format_admin_user_detail,
    format_admin_wallet_view,
    format_audit_logs_page,
    format_report_detail,
    handle_admin_command,
    handle_admin_config_confirm_callback,
    handle_admin_home_callback,
    handle_admin_wallet_confirm_callback,
)
from src.app.bot.handlers.base import get_base_router
from src.app.bot.handlers.chat import handle_chat_relay_message
from src.app.bot.handlers.moderation import (
    handle_report_category_callback,
    handle_report_description_input,
    handle_report_start_chat_callback,
)
from src.app.bot.keyboards.admin import (
    CB_ADM_CFG_CONFIRM_PREFIX,
    CB_ADM_CFG_LIST,
    CB_ADM_HOME,
    CB_ADM_PAIR_MENU,
    CB_ADM_ROLES,
    CB_ADM_WALLET_CONFIRM_PREFIX,
    CB_ADM_WALLET_MENU,
    get_admin_main_menu_keyboard,
    get_report_detail_keyboard,
    get_user_inspection_keyboard,
)
from src.app.bot.keyboards.moderation import (
    CB_REPORT_CAT_PREFIX,
    CB_REPORT_START_CHAT_PREFIX,
    get_report_category_keyboard,
)
from src.app.bot.states import AdminSG
from src.app.config.settings import Settings
from src.app.modules.admin.models import AdminUser, SystemConfiguration
from src.app.modules.admin.rbac import ROLE_MODERATOR, ROLE_SUPER_ADMIN, ROLE_SUPPORT
from src.app.modules.admin.schemas import (
    AdminAuditLogDTO,
    AdminAuditLogPageDTO,
    AdminUserDetailDTO,
    ReportDetailDTO,
)
from src.app.modules.chat.models import ActiveChatParticipant, ChatSession
from src.app.modules.moderation.models import Report
from src.app.modules.profiles.service import ProfileData, ProfileService
from src.app.modules.users.service import UserService
from src.app.modules.wallet.schemas import (
    LedgerEntryDTO,
    TransactionHistoryPageDTO,
    WalletBalanceDTO,
)
from src.app.modules.wallet.service import WalletService


class SQLiteDBWrapper:
    """SQLite DatabaseManager adapter for handler tests."""

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


def _make_message(telegram_id: int, text: str, bot: MagicMock | None = None) -> MagicMock:
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.caption = None
    msg.photo = None
    msg.voice = None
    msg.video = None
    msg.sticker = None
    msg.animation = None
    msg.audio = None
    msg.document = None
    tg_user = MagicMock(spec=TelegramUser)
    tg_user.id = telegram_id
    tg_user.is_bot = False
    msg.from_user = tg_user
    msg.answer = AsyncMock()
    msg.bot = bot or MagicMock(send_message=AsyncMock())
    return msg


def _make_callback(telegram_id: int, data: str, bot: MagicMock | None = None) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    tg_user = MagicMock(spec=TelegramUser)
    tg_user.id = telegram_id
    tg_user.is_bot = False
    cb.from_user = tg_user
    cb.answer = AsyncMock()
    cb.bot = bot or MagicMock(send_message=AsyncMock())
    msg = MagicMock(spec=Message)
    msg.answer = AsyncMock()
    msg.edit_reply_markup = AsyncMock()
    cb.message = msg
    return cb


def test_router_order_mounts_moderation_and_admin_before_chat() -> None:
    """Ensure ReportSG and /admin routers take priority ahead of chat_router catch-all."""
    router = get_base_router()
    names = [sub.name for sub in router.sub_routers]
    assert "moderation" in names
    assert "admin" in names
    assert names.index("moderation") < names.index("chat")
    assert names.index("admin") < names.index("chat")


def test_role_filtered_keyboards_and_64_byte_callback_limit() -> None:
    """Verify inline buttons respect RBAC visibility and Telegram 64-byte callback limit."""
    support_kb = get_admin_main_menu_keyboard(ROLE_SUPPORT)
    support_cbs = [btn.callback_data for row in support_kb.inline_keyboard for btn in row]
    assert CB_ADM_PAIR_MENU not in support_cbs
    assert CB_ADM_WALLET_MENU not in support_cbs
    assert CB_ADM_CFG_LIST not in support_cbs
    assert CB_ADM_ROLES not in support_cbs

    mod_kb = get_admin_main_menu_keyboard(ROLE_MODERATOR)
    mod_cbs = [btn.callback_data for row in mod_kb.inline_keyboard for btn in row]
    assert CB_ADM_PAIR_MENU in mod_cbs
    assert CB_ADM_WALLET_MENU not in mod_cbs
    assert CB_ADM_CFG_LIST not in mod_cbs

    super_kb = get_admin_main_menu_keyboard(ROLE_SUPER_ADMIN)
    super_cbs = [btn.callback_data for row in super_kb.inline_keyboard for btn in row]
    assert CB_ADM_WALLET_MENU in super_cbs
    assert CB_ADM_CFG_LIST in super_cbs
    assert CB_ADM_ROLES in super_cbs

    now = datetime.now(UTC)
    sample_user = AdminUserDetailDTO(
        user_id=9999,
        public_id="usr_abcdefghijkl",
        account_status="SUSPENDED",
        age_18_plus_declared=True,
        created_at=now,
        last_activity_at=now,
        suspended_until=now,
        suspension_reason="test",
        is_chat_restricted=True,
        chat_restriction_reason=None,
        chat_restriction_expires_at=None,
        admin_role=None,
        admin_is_active=None,
        display_name="Test",
        age=24,
        gender="MALE",
        city="Tehran",
        profile_completion_status="COMPLETED",
        profile_visibility="PUBLIC",
        in_active_chat=False,
        reports_against_count=1,
        reports_submitted_count=0,
        recent_actions=(),
    )
    sample_report = ReportDetailDTO(
        id=999999,
        category="HARASSMENT",
        status="PENDING",
        description="x",
        reporter_public_id="usr_reporter1234",
        reported_public_id="usr_reported5678",
        reported_account_status="ACTIVE",
        reported_is_chat_restricted=False,
        chat_session_id=uuid.uuid4(),
        reported_message_id=None,
        moderator_public_id=None,
        resolution_notes=None,
        created_at=now,
        reviewed_at=None,
        evidence_holds=(),
    )

    for kb in (
        support_kb,
        mod_kb,
        super_kb,
        get_user_inspection_keyboard(sample_user, ROLE_SUPER_ADMIN),
        get_report_detail_keyboard(sample_report, ROLE_SUPER_ADMIN),
        get_report_category_keyboard(),
    ):
        for row in kb.inline_keyboard:
            for btn in row:
                assert btn.callback_data is not None
                assert len(btn.callback_data.encode("utf-8")) <= 64


def test_admin_formatters_html_escape_and_never_leak_telegram_ids(
    test_settings: Settings,
) -> None:
    """Verify all admin outputs escape user strings and omit internal Telegram IDs."""
    now = datetime.now(UTC)
    user_dto = AdminUserDetailDTO(
        user_id=42,
        public_id="usr_leakcheck12",
        account_status="ACTIVE",
        age_18_plus_declared=True,
        created_at=now,
        last_activity_at=now,
        suspended_until=None,
        suspension_reason="<script>alert(1)</script>",
        is_chat_restricted=True,
        chat_restriction_reason="<b>Evil</b> & Reason",
        chat_restriction_expires_at=now,
        admin_role=None,
        admin_is_active=None,
        display_name="<u>User</u>",
        age=26,
        gender="FEMALE",
        city="<Tehran>",
        profile_completion_status="COMPLETED",
        profile_visibility="PUBLIC",
        in_active_chat=True,
        reports_against_count=2,
        reports_submitted_count=1,
        recent_actions=(),
    )
    rendered_user = format_admin_user_detail(user_dto, test_settings)
    assert "&lt;u&gt;User&lt;/u&gt;" in rendered_user
    assert "&lt;Tehran&gt;" in rendered_user
    assert "&lt;b&gt;Evil&lt;/b&gt; &amp; Reason" in rendered_user
    assert "telegram_user_id" not in rendered_user.lower()

    rep_dto = ReportDetailDTO(
        id=10,
        category="HARASSMENT",
        status="PENDING",
        description="<img src=x onerror=alert(1)> & abuse",
        reporter_public_id="usr_rep1",
        reported_public_id="usr_rep2",
        reported_account_status="ACTIVE",
        reported_is_chat_restricted=False,
        chat_session_id=None,
        reported_message_id=None,
        moderator_public_id=None,
        resolution_notes="<note>",
        created_at=now,
        reviewed_at=None,
        evidence_holds=(),
    )
    rendered_rep = format_report_detail(rep_dto, test_settings)
    assert "&lt;img src=x onerror=alert(1)&gt; &amp; abuse" in rendered_rep
    assert "&lt;note&gt;" in rendered_rep

    wal_view = format_admin_wallet_view(
        user_dto,
        WalletBalanceDTO(
            user_id=42,
            purchased_coins=10,
            free_coins=4,
            total_spendable_coins=14,
            free_allowance_id=None,
            free_allowance_date=None,
            free_expires_at=None,
        ),
        TransactionHistoryPageDTO(
            items=(
                LedgerEntryDTO(
                    id=1,
                    wallet_user_id=42,
                    idempotency_key="k",
                    transaction_type="ADMIN_GRANT",
                    credit_type="PURCHASED",
                    allowance_id=None,
                    allowance_date=None,
                    amount=10,
                    purchased_balance_before=0,
                    purchased_balance_after=10,
                    free_balance_before=4,
                    free_balance_after=4,
                    reference_id="admin:1",
                    actor_type="ADMIN",
                    actor_id=1,
                    reason="<bonus> & fix",
                    created_at=now,
                ),
            ),
            total_count=1,
            offset=0,
            limit=5,
            has_prev=False,
            has_next=False,
        ),
        test_settings,
    )
    assert "&lt;bonus&gt; &amp; fix" in wal_view

    audit_view = format_audit_logs_page(
        AdminAuditLogPageDTO(
            items=(
                AdminAuditLogDTO(
                    id=5,
                    admin_public_id="usr_adm",
                    action="SYSTEM_CONFIG_UPDATE",
                    target_type="SYSTEM_CONFIG",
                    target_id="random_chat_cost_coins",
                    payload_before={"coins": 1},
                    payload_after={"coins": 3},
                    reason="<reason> & tag",
                    created_at=now,
                ),
            ),
            total_count=1,
            offset=0,
            limit=5,
            has_prev=False,
            has_next=False,
        ),
        test_settings,
    )
    assert "&lt;reason&gt; &amp; tag" in audit_view


@pytest.mark.asyncio
async def test_unauthorized_admin_command_and_forged_callbacks_denied_cleanly(
    sqlite_engine: AsyncEngine,
    test_settings: Settings,
) -> None:
    """Verify non-admins receive denial without menu leakage and callback.answer is awaited."""
    db = SQLiteDBWrapper(sqlite_engine)
    await UserService(db).register_adult_user(810001, test_settings.business_timezone)  # type: ignore[arg-type]

    dp = Dispatcher(storage=MemoryStorage())
    state = dp.fsm.get_context(bot=MagicMock(), chat_id=810001, user_id=810001)
    await state.set_state(AdminSG.waiting_for_user_public_id)

    msg = _make_message(810001, "/admin")
    await handle_admin_command(msg, state, db_manager=db, settings=test_settings)  # type: ignore[arg-type]
    assert "دسترسی" in msg.answer.call_args[0][0]
    assert msg.answer.call_args.kwargs.get("reply_markup") is None
    assert await state.get_state() is None

    cb = _make_callback(810001, CB_ADM_HOME)
    await handle_admin_home_callback(cb, state, db_manager=db, settings=test_settings)  # type: ignore[arg-type]
    cb.answer.assert_awaited()
    assert "دسترسی" in cb.message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_two_step_wallet_and_config_confirmation_and_replay_rejection(
    sqlite_engine: AsyncEngine,
    test_settings: Settings,
) -> None:
    """Verify wallet & config confirmations execute exactly once and reject replay clicks."""
    db = SQLiteDBWrapper(sqlite_engine)
    u_svc = UserService(db)  # type: ignore[arg-type]
    admin, _ = await u_svc.register_adult_user(820001, test_settings.business_timezone)
    target, _ = await u_svc.register_adult_user(820002, test_settings.business_timezone)
    async with db.session() as session:
        session.add(AdminUser(user_id=admin.id, role=ROLE_SUPER_ADMIN, is_active=True))

    dp = Dispatcher(storage=MemoryStorage())
    state = dp.fsm.get_context(bot=MagicMock(), chat_id=820001, user_id=820001)

    # Wallet two-step confirm + replay rejection
    wal_token = "tok123abc"
    await state.set_state(AdminSG.confirming_wallet_op)
    await state.update_data(
        pending_confirm_token=wal_token,
        wallet_op="CREDIT",
        wallet_target_pid=target.public_id,
        wallet_amount=15,
        wallet_reason="Compensation grant",
        wallet_idempotency_key="tg_adm_test_idem_1",
    )

    cb1 = _make_callback(820001, f"{CB_ADM_WALLET_CONFIRM_PREFIX}{wal_token}")
    await handle_admin_wallet_confirm_callback(
        cb1,
        state,
        db_manager=db,
        settings=test_settings,  # type: ignore[arg-type]
    )
    assert "با موفقیت" in cb1.message.answer.call_args[0][0]
    assert await state.get_state() is None

    # Replay click must fail cleanly without crediting again
    cb_replay = _make_callback(820001, f"{CB_ADM_WALLET_CONFIRM_PREFIX}{wal_token}")
    await handle_admin_wallet_confirm_callback(
        cb_replay,
        state,
        db_manager=db,
        settings=test_settings,  # type: ignore[arg-type]
    )
    cb_replay.answer.assert_awaited()
    assert cb_replay.answer.call_args.kwargs.get("show_alert") is True

    bal = await WalletService(db, test_settings).get_wallet_balance(  # type: ignore[arg-type]
        target.id, auto_allocate_daily=False
    )
    assert bal.purchased_coins == 15

    # System configuration confirm + replay rejection
    cfg_token = "cfg789xyz"
    await state.set_state(AdminSG.confirming_config_update)
    await state.update_data(
        pending_confirm_token=cfg_token,
        cfg_key="random_chat_cost_coins",
        cfg_new_value=3,
        cfg_reason="Adjust tariff",
    )
    cb_cfg = _make_callback(820001, f"{CB_ADM_CFG_CONFIRM_PREFIX}{cfg_token}")
    await handle_admin_config_confirm_callback(
        cb_cfg,
        state,
        db_manager=db,
        settings=test_settings,  # type: ignore[arg-type]
    )
    assert "به‌روزرسانی شد" in cb_cfg.message.answer.call_args[0][0]

    cb_cfg_replay = _make_callback(820001, f"{CB_ADM_CFG_CONFIRM_PREFIX}{cfg_token}")
    await handle_admin_config_confirm_callback(
        cb_cfg_replay,
        state,
        db_manager=db,
        settings=test_settings,  # type: ignore[arg-type]
    )
    assert cb_cfg_replay.answer.call_args.kwargs.get("show_alert") is True

    async with db.session() as session:
        cfg_row = (
            await session.execute(
                select(SystemConfiguration).where(
                    SystemConfiguration.key == "random_chat_cost_coins"
                )
            )
        ).scalar_one()
        assert cfg_row.value == {"coins": 3}


@pytest.mark.asyncio
async def test_active_chat_report_flow_does_not_relay_report_text_to_partner(
    sqlite_engine: AsyncEngine,
    test_settings: Settings,
) -> None:
    """Verify reporting during an active chat submits Report and never relays to partner."""
    db = SQLiteDBWrapper(sqlite_engine)
    u_svc = UserService(db)  # type: ignore[arg-type]
    p_svc = ProfileService(db)  # type: ignore[arg-type]
    u1, _ = await u_svc.register_adult_user(830001, test_settings.business_timezone)
    u2, _ = await u_svc.register_adult_user(830002, test_settings.business_timezone)
    await p_svc.create_profile(830001, ProfileData(display_name="یک", age=24, gender="MALE"))
    await p_svc.create_profile(830002, ProfileData(display_name="دو", age=23, gender="FEMALE"))

    chat_id = uuid.uuid4()
    async with db.session() as session:
        session.add(
            ChatSession(
                id=chat_id,
                chat_type="RANDOM",
                participant_one_id=u1.id,
                participant_two_id=u2.id,
                status="ACTIVE",
            )
        )
        await session.flush()
        session.add_all(
            [
                ActiveChatParticipant(user_id=u1.id, chat_id=chat_id),
                ActiveChatParticipant(user_id=u2.id, chat_id=chat_id),
            ]
        )

    dp = Dispatcher(storage=MemoryStorage())
    state = dp.fsm.get_context(bot=MagicMock(), chat_id=830001, user_id=830001)
    bot = MagicMock(send_message=AsyncMock())

    # Step 1: start report from chat keyboard
    cb_start = _make_callback(830001, f"{CB_REPORT_START_CHAT_PREFIX}{chat_id.hex}", bot=bot)
    await handle_report_start_chat_callback(
        cb_start,
        state,
        db_manager=db,
        settings=test_settings,  # type: ignore[arg-type]
    )

    # Step 2: choose category
    cb_cat = _make_callback(830001, f"{CB_REPORT_CAT_PREFIX}HARASSMENT", bot=bot)
    await handle_report_category_callback(cb_cat, state)

    # Step 3: enter description while still in active chat
    msg_desc = _make_message(830001, "این کاربر پیام تهدیدآمیز فرستاد", bot=bot)
    await handle_report_description_input(
        msg_desc,
        state,
        db_manager=db,
        settings=test_settings,  # type: ignore[arg-type]
    )
    bot.send_message.assert_not_awaited()
    assert "ثبت شد" in msg_desc.answer.call_args[0][0]

    # Step 4: Verify normal chat relay works cleanly once state cleared
    msg_normal = _make_message(830001, "سلام", bot=bot)
    await handle_chat_relay_message(msg_normal, db_manager=db, settings=test_settings)  # type: ignore[arg-type]
    bot.send_message.assert_awaited_once()

    async with db.session() as session:
        rep = (await session.execute(select(Report))).scalar_one()
        assert rep.reported_user_id == u2.id
        assert rep.description == "این کاربر پیام تهدیدآمیز فرستاد"
