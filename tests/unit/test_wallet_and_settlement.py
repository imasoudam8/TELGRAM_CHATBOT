"""Unit tests for WalletService, Tehran midnight/DST boundaries, settlement, and wallet handlers."""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import CallbackQuery, Message
from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from src.app.bot.handlers.chat import handle_chat_relay_message, handle_end_chat_message
from src.app.bot.handlers.wallet import (
    handle_wallet_history_callback,
    handle_wallet_menu_entry,
)
from src.app.bot.keyboards.onboarding import BTN_WALLET
from src.app.bot.keyboards.wallet import CB_WALLET_HISTORY_PREFIX
from src.app.config.settings import Settings
from src.app.modules.chat.models import ActiveChatParticipant, ChatSession, DirectRequest
from src.app.modules.users.service import UserService
from src.app.modules.wallet.models import WalletLedger
from src.app.modules.wallet.schemas import (
    InsufficientBalanceError,
    InvalidAmountError,
    WalletPermissionError,
)
from src.app.modules.wallet.service import WalletService, get_tehran_midnight_expiry
from src.app.modules.wallet.settlement import ChatSettlementService


class SQLiteDBWrapper:
    """SQLite DatabaseManager adapter for unit testing."""

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


def test_tehran_midnight_and_dst_transition_boundaries() -> None:
    """Verify exact UTC expiry at Asia/Tehran midnight including historical spring-forward DST."""
    tz = ZoneInfo("Asia/Tehran")

    # Standard non-DST day: 2026-11-12 ends at 2026-11-13 00:00:00+03:30 == 2026-11-12 20:30:00Z
    exp_std = get_tehran_midnight_expiry(date(2026, 11, 12), tz)
    assert exp_std == datetime(2026, 11, 12, 20, 30, 0, tzinfo=UTC)

    # Historical Iran DST spring-forward boundary on 2022-03-22 (00:00 jumped to 01:00+04:30)
    # 2022-03-21 ends at 2022-03-21 20:30:00Z (exact first instant of 2022-03-22 in Tehran)
    exp_dst = get_tehran_midnight_expiry(date(2022, 3, 21), tz)
    assert exp_dst == datetime(2022, 3, 21, 20, 30, 0, tzinfo=UTC)
    assert exp_dst.astimezone(tz).date() == date(2022, 3, 22)


@pytest.mark.asyncio
async def test_wallet_creation_purchased_credit_debit_split_and_validation(
    sqlite_engine: AsyncEngine,
    test_settings: Settings,
) -> None:
    """Verify wallet zero init, positive amount checks, free-first split debit, and history."""
    db = SQLiteDBWrapper(sqlite_engine)
    user_svc = UserService(db)
    admin, _ = await user_svc.register_adult_user(900001, test_settings.business_timezone)
    user, _ = await user_svc.register_adult_user(900002, test_settings.business_timezone)
    other, _ = await user_svc.register_adult_user(900003, test_settings.business_timezone)

    custom_settings = test_settings.model_copy(update={"DAILY_FREE_COINS_QUOTA": 3})
    wallet_svc = WalletService(db, custom_settings)

    # 1. Wallet creation & initial zero balances
    init_bal = await wallet_svc.ensure_wallet(user.id)
    assert init_bal.purchased_coins == 0
    assert init_bal.free_coins == 0
    assert init_bal.total_spendable_coins == 0

    # 2. Reject zero / negative amounts & missing reasons
    with pytest.raises(InvalidAmountError):
        await wallet_svc.credit_purchased_coins(
            user.id, 0, idempotency_key="bad_zero", reason="Invalid"
        )
    with pytest.raises(InvalidAmountError):
        await wallet_svc.debit_coins(user.id, -5, idempotency_key="bad_neg", reason="Invalid")
    with pytest.raises(ValueError):
        await wallet_svc.admin_credit_coins(
            user.id,
            5,
            admin_actor_id=admin.id,
            reason="   ",
            idempotency_key="bad_reason",
        )

    # 3. Admin grant 5 purchased coins + idempotency check
    ts1 = datetime(2026, 11, 12, 10, 0, 0, tzinfo=UTC)
    bal_after_grant, entry_grant = await wallet_svc.admin_credit_coins(
        user.id,
        5,
        admin_actor_id=admin.id,
        reason="Support top-up",
        idempotency_key="adm_grant_1",
        utc_now=ts1,
    )
    assert bal_after_grant.purchased_coins == 5
    assert entry_grant.actor_type == "ADMIN"
    assert entry_grant.actor_id == admin.id
    assert entry_grant.credit_type == "PURCHASED"
    assert entry_grant.allowance_id is None

    # Duplicate idempotency key does NOT credit twice
    bal_dup, entry_dup = await wallet_svc.admin_credit_coins(
        user.id,
        5,
        admin_actor_id=admin.id,
        reason="Support top-up retry",
        idempotency_key="adm_grant_1",
        utc_now=ts1,
    )
    assert bal_dup.purchased_coins == 5
    assert entry_dup.id == entry_grant.id

    # Cross-user idempotency key collision rejected
    with pytest.raises(WalletPermissionError):
        await wallet_svc.credit_purchased_coins(
            other.id, 5, idempotency_key="adm_grant_1", reason="Wrong user"
        )

    # 4. Allocate daily free quota (3 coins), then debit 4 coins -> consumes 3 FREE + 1 PURCHASED
    await wallet_svc.allocate_daily_allowance(user.id, utc_now=ts1)
    bal_split, ledgers_split = await wallet_svc.debit_coins(
        user.id,
        4,
        idempotency_key="split_debit_1",
        reason="Mixed debit",
        utc_now=ts1,
    )
    assert len(ledgers_split) == 2
    assert ledgers_split[0].credit_type == "FREE"
    assert ledgers_split[0].amount == 3
    assert ledgers_split[0].free_balance_before == 3
    assert ledgers_split[0].free_balance_after == 0
    assert ledgers_split[0].allowance_id is not None

    assert ledgers_split[1].credit_type == "PURCHASED"
    assert ledgers_split[1].amount == 1
    assert ledgers_split[1].purchased_balance_before == 5
    assert ledgers_split[1].purchased_balance_after == 4
    assert ledgers_split[1].allowance_id is None

    assert bal_split.free_coins == 0
    assert bal_split.purchased_coins == 4
    assert bal_split.total_spendable_coins == 4

    # 5. Insufficient balance rejected cleanly without negative balance
    with pytest.raises(InsufficientBalanceError):
        await wallet_svc.debit_coins(
            user.id,
            10,
            idempotency_key="overdraft_1",
            reason="Too much",
            utc_now=ts1,
        )

    # 6. Tehran midnight expiration & no carryover to next day
    ts_next_day = datetime(2026, 11, 12, 20, 30, 0, tzinfo=UTC)  # 00:00:00 Tehran on 2026-11-13
    # Even if user had unused free coins on day 1 (test on `other`), day 2 sees 0 until allocated
    await wallet_svc.allocate_daily_allowance(other.id, utc_now=ts1)
    bal_other_before = await wallet_svc.get_wallet_balance(other.id, utc_now=ts1)
    assert bal_other_before.free_coins == 3
    bal_other_expired = await wallet_svc.get_wallet_balance(other.id, utc_now=ts_next_day)
    assert bal_other_expired.free_coins == 0
    assert bal_other_expired.total_spendable_coins == 0

    # Paginated transaction history
    page0 = await wallet_svc.get_transaction_history(user.id, offset=0, limit=2)
    assert page0.total_count == 4  # ADMIN_GRANT, DAILY_GRANT, split:free, split:purchased
    assert len(page0.items) == 2
    assert page0.has_next is True
    assert page0.has_prev is False


@pytest.mark.asyncio
async def test_failed_first_delivery_does_not_charge_on_message_or_subsequent_end(
    sqlite_engine: AsyncEngine,
    test_settings: Settings,
) -> None:
    """Verify failed Telegram delivery on P2's 1st message avoids charging even on chat end."""
    db = SQLiteDBWrapper(sqlite_engine)
    user_svc = UserService(db)
    u1, _ = await user_svc.register_adult_user(910001, test_settings.business_timezone)
    u2, _ = await user_svc.register_adult_user(910002, test_settings.business_timezone)

    chat_id = uuid.uuid4()
    async with db.session() as session:
        session.add(
            ChatSession(
                id=chat_id,
                chat_type="RANDOM",
                participant_one_id=u1.id,
                participant_two_id=u2.id,
                status="ACTIVE",
                is_charged=False,
            )
        )
        await session.flush()
        session.add_all(
            [
                ActiveChatParticipant(user_id=u1.id, chat_id=chat_id),
                ActiveChatParticipant(user_id=u2.id, chat_id=chat_id),
            ]
        )

    def _msg(tg_id: int, text: str, bot: MagicMock) -> MagicMock:
        m = MagicMock(spec=Message)
        m.text = text
        m.caption = None
        m.photo = None
        m.voice = None
        m.video = None
        m.sticker = None
        m.animation = None
        m.audio = None
        m.document = None
        tu = MagicMock(spec=TelegramUser)
        tu.id = tg_id
        tu.is_bot = False
        m.from_user = tu
        m.answer = AsyncMock()
        m.bot = bot
        return m

    ok_bot = MagicMock()
    ok_bot.send_message = AsyncMock()
    await handle_chat_relay_message(
        _msg(910001, "سلام", ok_bot), db_manager=db, settings=test_settings
    )

    fail_bot = MagicMock()
    fail_bot.send_message = AsyncMock(side_effect=RuntimeError("Telegram Forbidden"))
    await handle_chat_relay_message(
        _msg(910002, "سلام برگشتی", fail_bot), db_manager=db, settings=test_settings
    )

    state = MagicMock()
    state.clear = AsyncMock()
    await handle_end_chat_message(
        _msg(910001, "/end", ok_bot), state=state, db_manager=db, settings=test_settings
    )

    async with db.session() as session:
        cs = (
            await session.execute(select(ChatSession).where(ChatSession.id == chat_id))
        ).scalar_one()
        assert cs.status == "ENDED"
        assert cs.is_charged is False
        assert cs.charged_at is None
        settlement_rows = (
            (
                await session.execute(
                    select(WalletLedger).where(WalletLedger.transaction_type == "CHAT_SETTLEMENT")
                )
            )
            .scalars()
            .all()
        )
        assert settlement_rows == []


@pytest.mark.asyncio
async def test_direct_chat_settlement_charges_requester_2_and_recipient_0(
    sqlite_engine: AsyncEngine,
    test_settings: Settings,
) -> None:
    """Verify DIRECT chat charges requester 2 coins and recipient 0 only after both send."""
    db = SQLiteDBWrapper(sqlite_engine)
    user_svc = UserService(db)
    req, _ = await user_svc.register_adult_user(920001, test_settings.business_timezone)
    tgt, _ = await user_svc.register_adult_user(920002, test_settings.business_timezone)

    no_free_settings = test_settings.model_copy(update={"DAILY_FREE_COINS_QUOTA": 0})
    w_svc = WalletService(db, no_free_settings)
    await w_svc.credit_purchased_coins(
        req.id, 5, idempotency_key="req_seed", reason="Requester seed"
    )
    await w_svc.credit_purchased_coins(
        tgt.id, 5, idempotency_key="tgt_seed", reason="Recipient seed"
    )

    chat_id = uuid.uuid4()
    async with db.session() as session:
        dr = DirectRequest(
            requester_id=req.id,
            target_id=tgt.id,
            status="ACCEPTED",
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
        session.add(dr)
        await session.flush()
        session.add(
            ChatSession(
                id=chat_id,
                chat_type="DIRECT",
                direct_request_id=dr.id,
                direct_request_status="ACCEPTED",
                participant_one_id=req.id,
                participant_two_id=tgt.id,
                status="ACTIVE",
                participant_one_sent_msg=True,
                participant_two_sent_msg=False,
                is_charged=False,
            )
        )

    settle_svc = ChatSettlementService(db, no_free_settings)
    res_early = await settle_svc.settle_chat(chat_id, auto_allocate_daily=False)
    assert res_early.status == "NOT_ELIGIBLE"

    async with db.session() as session:
        cs = (
            await session.execute(select(ChatSession).where(ChatSession.id == chat_id))
        ).scalar_one()
        cs.participant_two_sent_msg = True

    res_paid = await settle_svc.settle_chat(chat_id, auto_allocate_daily=False)
    assert res_paid.status == "SETTLED"
    assert res_paid.total_coins_charged == 2
    assert res_paid.coins_by_user == {req.id: 2}

    bal_req = await w_svc.get_wallet_balance(req.id)
    bal_tgt = await w_svc.get_wallet_balance(tgt.id)
    assert bal_req.purchased_coins == 3
    assert bal_tgt.purchased_coins == 5


@pytest.mark.asyncio
async def test_wallet_bot_handlers_render_balance_and_history(
    sqlite_engine: AsyncEngine,
    test_settings: Settings,
) -> None:
    """Verify Telegram wallet button & history callback render Persian summary cleanly."""
    db = SQLiteDBWrapper(sqlite_engine)
    user_svc = UserService(db)
    user, _ = await user_svc.register_adult_user(930001, test_settings.business_timezone)

    state = MagicMock()
    state.clear = AsyncMock()

    msg = MagicMock(spec=Message)
    msg.text = BTN_WALLET
    tu = MagicMock(spec=TelegramUser)
    tu.id = 930001
    tu.is_bot = False
    msg.from_user = tu
    msg.answer = AsyncMock()

    await handle_wallet_menu_entry(msg, state=state, db_manager=db, settings=test_settings)
    msg.answer.assert_awaited_once()
    reply_text = msg.answer.call_args[0][0]
    assert "کیف پول سکه‌های چت شما" in reply_text
    assert f"<b>{test_settings.DAILY_FREE_COINS_QUOTA}</b>" in reply_text
    assert str(930001) not in reply_text

    # History callback shows DAILY_GRANT entry
    cb = MagicMock(spec=CallbackQuery)
    cb.data = f"{CB_WALLET_HISTORY_PREFIX}0"
    cb.from_user = tu
    cb.answer = AsyncMock()
    cb_msg = MagicMock(spec=Message)
    cb_msg.answer = AsyncMock()
    cb.message = cb_msg

    await handle_wallet_history_callback(cb, db_manager=db, settings=test_settings)
    cb.answer.assert_awaited_once()
    hist_text = cb_msg.answer.call_args[0][0]
    assert "سهمیه رایگان روزانه" in hist_text
    assert user.public_id not in hist_text
