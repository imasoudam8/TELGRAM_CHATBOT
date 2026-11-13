"""Live PostgreSQL 16 integration tests for TASK008 wallet, daily allowances, and settlement."""

import asyncio
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import func, select, text

from src.app.config.settings import Settings
from src.app.infrastructure.database.session import DatabaseManager
from src.app.modules.chat.models import ActiveChatParticipant, ChatSession, DirectRequest
from src.app.modules.chat.session_service import ChatService
from src.app.modules.profiles.service import ProfileData, ProfileService
from src.app.modules.users.service import UserService
from src.app.modules.wallet.models import WalletDailyAllowance, WalletLedger
from src.app.modules.wallet.schemas import InsufficientBalanceError
from src.app.modules.wallet.service import WalletService
from src.app.modules.wallet.settlement import ChatSettlementService


@pytest.mark.asyncio
async def test_live_postgres_wallet_daily_expiry_concurrency_and_append_only_ledger(
    migrated_postgres_db: tuple[str, Settings],
) -> None:
    """Verify scenarios 1-10 & 16 on live PostgreSQL 16 with concurrent tasks."""
    _, settings = migrated_postgres_db
    custom_settings = settings.model_copy(update={"DAILY_FREE_COINS_QUOTA": 4})
    db = DatabaseManager(custom_settings)
    await db.initialize()

    try:
        user_svc = UserService(db)
        u1, _ = await user_svc.register_adult_user(950001, custom_settings.business_timezone)
        u2, _ = await user_svc.register_adult_user(950002, custom_settings.business_timezone)
        admin, _ = await user_svc.register_adult_user(950003, custom_settings.business_timezone)

        w_svc = WalletService(db, custom_settings)

        # Scenario 1: initial zero balances
        b_init = await w_svc.ensure_wallet(u1.id)
        assert b_init.purchased_coins == 0
        assert b_init.free_coins == 0
        assert b_init.total_spendable_coins == 0

        # Scenario 9: Concurrent daily allocation for u1 at 23:59:59 Tehran (20:29:59 UTC)
        day1_before_midnight = datetime(2026, 11, 12, 20, 29, 59, tzinfo=UTC)
        results = await asyncio.gather(
            *[w_svc.allocate_daily_allowance(u1.id, utc_now=day1_before_midnight) for _ in range(8)]
        )
        for res in results:
            assert res.free_coins == 4

        # Simulate app restart (brand new WalletService instance) -> still allocated only once
        w_svc_restarted = WalletService(db, custom_settings)
        res_restarted = await w_svc_restarted.allocate_daily_allowance(
            u1.id, utc_now=day1_before_midnight
        )
        assert res_restarted.free_coins == 4

        async with db.session() as session:
            allowance_count = (
                await session.execute(
                    select(func.count(WalletDailyAllowance.id)).where(
                        WalletDailyAllowance.user_id == u1.id
                    )
                )
            ).scalar_one()
            grant_ledger_count = (
                await session.execute(
                    select(func.count(WalletLedger.id)).where(
                        WalletLedger.wallet_user_id == u1.id,
                        WalletLedger.transaction_type == "DAILY_GRANT",
                    )
                )
            ).scalar_one()
            assert allowance_count == 1
            assert grant_ledger_count == 1

        # Scenarios 5 & 6: 1 second later at 00:00:00 Tehran (20:30:00 UTC) -> expired
        day2_midnight = datetime(2026, 11, 12, 20, 30, 0, tzinfo=UTC)
        b_day2_expired = await w_svc.get_wallet_balance(
            u1.id, auto_allocate_daily=False, utc_now=day2_midnight
        )
        assert b_day2_expired.free_coins == 0
        assert b_day2_expired.total_spendable_coins == 0

        # Allocating on day 2 creates fresh 4-coin quota (NOT 8)
        b_day2_fresh = await w_svc.allocate_daily_allowance(u1.id, utc_now=day2_midnight)
        assert b_day2_fresh.free_coins == 4

        # Scenario 2 & 3: Credit 2 purchased coins to u2, verify free consumed before purchased
        await w_svc.credit_purchased_coins(
            u2.id, 2, idempotency_key="pg_buy_u2", reason="Purchased pack", utc_now=day2_midnight
        )
        await w_svc.allocate_daily_allowance(u2.id, utc_now=day2_midnight)
        bal_u2_after_5, split_ledgers = await w_svc.debit_coins(
            u2.id,
            5,
            idempotency_key="pg_u2_spend_5",
            reason="Spend 5 coins",
            utc_now=day2_midnight,
        )
        assert len(split_ledgers) == 2
        assert bal_u2_after_5.free_coins == 0
        assert bal_u2_after_5.purchased_coins == 1

        # Scenario 7: Replaying split debit idempotency key does not mutate again
        bal_u2_replay, replay_ledgers = await w_svc.debit_coins(
            u2.id,
            5,
            idempotency_key="pg_u2_spend_5",
            reason="Spend 5 coins replay",
            utc_now=day2_midnight,
        )
        assert bal_u2_replay.purchased_coins == 1
        assert [e.id for e in replay_ledgers] == [e.id for e in split_ledgers]

        # Scenario 10: Concurrent wallet debits on u1 (4 free + 0 purchased = 4 total)
        # Launch 6 concurrent debits of 1 coin -> 4 succeed, 2 fail with InsufficientBalanceError
        concurrent_debits = await asyncio.gather(
            *[
                w_svc.debit_coins(
                    u1.id,
                    1,
                    idempotency_key=f"conc_debit_{idx}",
                    reason=f"Concurrent debit {idx}",
                    utc_now=day2_midnight,
                )
                for idx in range(6)
            ],
            return_exceptions=True,
        )
        ok_count = sum(1 for r in concurrent_debits if not isinstance(r, Exception))
        fail_count = sum(1 for r in concurrent_debits if isinstance(r, InsufficientBalanceError))
        assert ok_count == 4
        assert fail_count == 2

        bal_u1_final = await w_svc.get_wallet_balance(u1.id, utc_now=day2_midnight)
        assert bal_u1_final.free_coins == 0
        assert bal_u1_final.purchased_coins == 0
        assert bal_u1_final.total_spendable_coins == 0

        # Scenario 16: PostgreSQL append-only trigger rejects UPDATE and DELETE on wallet_ledgers
        with pytest.raises(Exception) as upd_exc:
            async with db.session() as session:
                await session.execute(
                    text(
                        "UPDATE wallet_ledgers SET reason = 'tamper' "
                        f"WHERE wallet_user_id = {u1.id}"
                    )
                )
        assert "append-only" in str(upd_exc.value).lower()

        with pytest.raises(Exception) as del_exc:
            async with db.session() as session:
                await session.execute(
                    text(f"DELETE FROM wallet_ledgers WHERE wallet_user_id = {u1.id}")
                )
        assert "append-only" in str(del_exc.value).lower()

        # Admin debit verification
        bal_u2_admin, adm_ledgers = await w_svc.admin_debit_coins(
            u2.id,
            1,
            admin_actor_id=admin.id,
            reason="Refund clawback",
            idempotency_key="adm_debit_u2",
            utc_now=day2_midnight,
        )
        assert bal_u2_admin.purchased_coins == 0
        assert adm_ledgers[0].actor_id == admin.id
        assert adm_ledgers[0].actor_type == "ADMIN"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_live_postgres_random_and_direct_chat_settlement_concurrency_rollback_and_retry(
    migrated_postgres_db: tuple[str, Settings],
) -> None:
    """Verify scenarios 11-18 on live PostgreSQL 16 under concurrency, rollback, and retry."""
    _, settings = migrated_postgres_db
    no_free_settings = settings.model_copy(update={"DAILY_FREE_COINS_QUOTA": 0})
    db = DatabaseManager(no_free_settings)
    await db.initialize()

    try:
        user_svc = UserService(db)
        prof_svc = ProfileService(db)
        u1, _ = await user_svc.register_adult_user(960001, no_free_settings.business_timezone)
        u2, _ = await user_svc.register_adult_user(960002, no_free_settings.business_timezone)
        u3, _ = await user_svc.register_adult_user(960003, no_free_settings.business_timezone)
        for tg_id, name, g in (
            (960001, "یک", "MALE"),
            (960002, "دو", "FEMALE"),
            (960003, "سه", "FEMALE"),
        ):
            await prof_svc.create_profile(
                tg_id, ProfileData(display_name=name, age=25, gender=g, city="تهران")
            )

        w_svc = WalletService(db, no_free_settings)
        settle_svc = ChatSettlementService(db, no_free_settings)
        chat_svc = ChatService(db, no_free_settings)

        # Fund u1 (10) and u2 (10); leave u3 with 0 coins
        await w_svc.credit_purchased_coins(u1.id, 10, idempotency_key="seed_u1", reason="Seed u1")
        await w_svc.credit_purchased_coins(u2.id, 10, idempotency_key="seed_u2", reason="Seed u2")

        # --- Case A: RANDOM chat (u1 + u2) -> Scenario 15, 11, 12 ---
        random_chat_id = uuid.uuid4()
        async with db.session() as session:
            session.add(
                ChatSession(
                    id=random_chat_id,
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
                    ActiveChatParticipant(user_id=u1.id, chat_id=random_chat_id),
                    ActiveChatParticipant(user_id=u2.id, chat_id=random_chat_id),
                ]
            )

        # Scenario 15: Only u1 sends -> NOT_ELIGIBLE, not charged
        await chat_svc.prepare_and_record_message(
            sender_user_id=u1.id, message_type="TEXT", text_content="سلام دو"
        )
        not_ready = await settle_svc.settle_chat(random_chat_id, auto_allocate_daily=False)
        assert not_ready.status == "NOT_ELIGIBLE"

        # u2 replies -> both sent flags True
        await chat_svc.prepare_and_record_message(
            sender_user_id=u2.id, message_type="TEXT", text_content="سلام یک"
        )

        # Scenario 11 & 12: 6 concurrent settlement attempts -> exactly 1 SETTLED, 5 ALREADY_CHARGED
        race_results = await asyncio.gather(
            *[settle_svc.settle_chat(random_chat_id, auto_allocate_daily=False) for _ in range(6)]
        )
        statuses = [r.status for r in race_results]
        assert statuses.count("SETTLED") == 1
        assert statuses.count("ALREADY_CHARGED") == 5

        assert (await w_svc.get_wallet_balance(u1.id)).purchased_coins == 9
        assert (await w_svc.get_wallet_balance(u2.id)).purchased_coins == 9

        await chat_svc.end_chat(u1.id, reason="USER_LEFT")

        # --- Case B: RANDOM chat (u1 + u3 where u3 has 0 coins) -> Scenario 13 ---
        insufficient_chat_id = uuid.uuid4()
        async with db.session() as session:
            session.add(
                ChatSession(
                    id=insufficient_chat_id,
                    chat_type="RANDOM",
                    participant_one_id=u1.id,
                    participant_two_id=u3.id,
                    status="ACTIVE",
                    participant_one_sent_msg=True,
                    participant_two_sent_msg=True,
                    is_charged=False,
                )
            )
            await session.flush()
            session.add_all(
                [
                    ActiveChatParticipant(user_id=u1.id, chat_id=insufficient_chat_id),
                    ActiveChatParticipant(user_id=u3.id, chat_id=insufficient_chat_id),
                ]
            )

        res_insuff = await settle_svc.settle_chat(insufficient_chat_id, auto_allocate_daily=False)
        assert res_insuff.status == "INSUFFICIENT_BALANCE"
        assert res_insuff.insufficient_user_ids == (u3.id,)
        # u1 was NOT charged when u3 lacked funds!
        assert (await w_svc.get_wallet_balance(u1.id)).purchased_coins == 9

        # Ending a chat when insufficient balance still allows leaving cleanly
        end_insuff = await chat_svc.end_chat(u1.id, reason="USER_LEFT")
        assert end_insuff is not None
        async with db.session() as session:
            cs_insuff = (
                await session.execute(
                    select(ChatSession).where(ChatSession.id == insufficient_chat_id)
                )
            ).scalar_one()
            assert cs_insuff.status == "ENDED"
            assert cs_insuff.is_charged is False
            assert cs_insuff.charged_at is None
            assert cs_insuff.participant_one_sent_msg is True
            assert cs_insuff.participant_two_sent_msg is True

        # --- Case C: DIRECT chat (u1 -> u2) + Rollback (#17) + Retry (#18) + Scenario 14 ---
        direct_chat_id = uuid.uuid4()
        async with db.session() as session:
            dr = DirectRequest(
                requester_id=u1.id,
                target_id=u2.id,
                status="ACCEPTED",
                expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            )
            session.add(dr)
            await session.flush()
            session.add(
                ChatSession(
                    id=direct_chat_id,
                    chat_type="DIRECT",
                    direct_request_id=dr.id,
                    direct_request_status="ACCEPTED",
                    participant_one_id=u1.id,
                    participant_two_id=u2.id,
                    status="ACTIVE",
                    participant_one_sent_msg=True,
                    participant_two_sent_msg=True,
                    is_charged=False,
                )
            )

        # Scenario 17: Simulate DB failure after debit before commit -> clean rollback
        orig_debit = settle_svc._wallet_service.debit_coins_in_session

        async def _boom_after_debit(*args, **kwargs):
            await orig_debit(*args, **kwargs)
            raise RuntimeError("Simulated mid-transaction failure")

        with patch.object(
            settle_svc._wallet_service, "debit_coins_in_session", side_effect=_boom_after_debit
        ):
            with pytest.raises(RuntimeError, match="Simulated mid-transaction failure"):
                await settle_svc.settle_chat(direct_chat_id, auto_allocate_daily=False)

        assert (await w_svc.get_wallet_balance(u1.id)).purchased_coins == 9
        async with db.session() as session:
            cs_rolled_back = (
                await session.execute(select(ChatSession).where(ChatSession.id == direct_chat_id))
            ).scalar_one()
            assert cs_rolled_back.is_charged is False
            assert cs_rolled_back.charged_at is None
            direct_ledgers = (
                await session.execute(
                    select(func.count(WalletLedger.id)).where(
                        WalletLedger.reference_id == str(direct_chat_id)
                    )
                )
            ).scalar_one()
            assert direct_ledgers == 0

        # Scenario 18 & 14: Retry succeeds -> u1 charged 2 coins (9 -> 7), u2 charged 0 (stays 9)
        retry_res = await settle_svc.settle_chat(direct_chat_id, auto_allocate_daily=False)
        assert retry_res.status == "SETTLED"
        assert retry_res.total_coins_charged == 2
        assert retry_res.coins_by_user == {u1.id: 2}
        assert (await w_svc.get_wallet_balance(u1.id)).purchased_coins == 7
        assert (await w_svc.get_wallet_balance(u2.id)).purchased_coins == 9

        # Scenario: Ending a chat settles pending valid charges when not yet charged
        unsettled_random_id = uuid.uuid4()
        async with db.session() as session:
            session.add(
                ChatSession(
                    id=unsettled_random_id,
                    chat_type="RANDOM",
                    participant_one_id=u1.id,
                    participant_two_id=u2.id,
                    status="ACTIVE",
                    participant_one_sent_msg=True,
                    participant_two_sent_msg=True,
                    is_charged=False,
                )
            )
            await session.flush()
            session.add_all(
                [
                    ActiveChatParticipant(user_id=u1.id, chat_id=unsettled_random_id),
                    ActiveChatParticipant(user_id=u2.id, chat_id=unsettled_random_id),
                ]
            )
        await chat_svc.end_chat(u1.id, reason="USER_LEFT")
        assert (await w_svc.get_wallet_balance(u1.id)).purchased_coins == 6
        assert (await w_svc.get_wallet_balance(u2.id)).purchased_coins == 8
        async with db.session() as session:
            cs_settled_on_end = (
                await session.execute(
                    select(ChatSession).where(ChatSession.id == unsettled_random_id)
                )
            ).scalar_one()
            assert cs_settled_on_end.is_charged is True
            assert cs_settled_on_end.status == "ENDED"
    finally:
        await db.close()
