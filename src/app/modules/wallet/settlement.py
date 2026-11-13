"""Atomic chat settlement service integrating ChatSession lifecycle with WalletService.

Unresolved Product Policy Notice:
    When one or both required participants lack sufficient spendable coins at the moment
    both participants have sent at least one message, the product policy for how to handle
    the chat (e.g., immediate chat pause/end, warning prompt, or deferred debt collection)
    is intentionally NOT invented here. Instead, settlement returns an explicit
    ``SettlementResult(status="INSUFFICIENT_BALANCE", ...)`` with zero balance mutations,
    zero settlement ledger rows, ``chat_sessions.is_charged = False``, and both
    ``participant_one_sent_msg = True`` / ``participant_two_sent_msg = True`` preserved so
    a future policy handler or reconciliation job can resolve pending settlements.
"""

import logging
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.config.settings import Settings, get_settings
from src.app.infrastructure.database.base import get_business_date, now_utc
from src.app.infrastructure.database.session import DatabaseManager
from src.app.modules.admin.config_service import get_effective_chat_tariffs_in_session
from src.app.modules.chat.models import ChatSession
from src.app.modules.wallet.schemas import SettlementResult
from src.app.modules.wallet.service import WalletService, _as_utc, _build_balance_dto

logger = logging.getLogger(__name__)

RANDOM_CHAT_COST_COINS = 1
DIRECT_CHAT_COST_COINS = 2


def get_settlement_charges_for_chat(chat: ChatSession) -> dict[int, int]:
    """Return required coin charge per participant according to accepted pricing rules.

    - RANDOM: 1 coin per participant.
    - DIRECT: 2 coins for requester (participant_one_id); 0 for recipient.
    """
    if chat.chat_type == "RANDOM":
        return {
            chat.participant_one_id: RANDOM_CHAT_COST_COINS,
            chat.participant_two_id: RANDOM_CHAT_COST_COINS,
        }
    if chat.chat_type == "DIRECT":
        return {chat.participant_one_id: DIRECT_CHAT_COST_COINS}
    raise ValueError(f"نوع گفتگوی نامعتبر برای تسویه: {chat.chat_type}")


async def get_settlement_charges_for_chat_in_session(
    session: AsyncSession,
    chat: ChatSession,
) -> dict[int, int]:
    """Resolve effective coin charges from SystemConfiguration honoring defaults."""
    random_cost, direct_cost = await get_effective_chat_tariffs_in_session(session)
    if chat.chat_type == "RANDOM":
        return {
            chat.participant_one_id: random_cost,
            chat.participant_two_id: random_cost,
        }
    if chat.chat_type == "DIRECT":
        return {chat.participant_one_id: direct_cost}
    raise ValueError(f"نوع گفتگوی نامعتبر برای تسویه: {chat.chat_type}")


class ChatSettlementService:
    """Coordinates idempotent, concurrency-safe PostgreSQL chat settlement."""

    def __init__(self, db_manager: DatabaseManager, settings: Settings | None = None) -> None:
        self._db_manager = db_manager
        self._settings = settings or get_settings()
        self._wallet_service = WalletService(db_manager, self._settings)

    async def get_effective_tariffs(self) -> tuple[int, int]:
        """Return active (random_chat_cost_coins, direct_chat_cost_coins)."""
        async with self._db_manager.session() as session:
            return await get_effective_chat_tariffs_in_session(session)

    async def settle_chat_in_session(
        self,
        session: AsyncSession,
        chat: ChatSession,
        *,
        auto_allocate_daily: bool = True,
        utc_now: datetime | None = None,
    ) -> SettlementResult:
        """Settle an already-locked ChatSession inside an existing PostgreSQL transaction.

        Assumes the caller holds ``FOR UPDATE`` on ``chat``.
        Locks wallets and daily allowances in deterministic ascending ``user_id`` order.
        Never marks ``chat.is_charged = True`` unless all required debits and ledger entries
        succeed in the same transaction.
        """
        ts = _as_utc(utc_now) if utc_now is not None else now_utc()
        business_date = get_business_date(ts, self._wallet_service.business_tz)

        if chat.is_charged:
            return SettlementResult(
                chat_id=chat.id,
                status="ALREADY_CHARGED",
                chat_type=chat.chat_type,
                charged_at=_as_utc(chat.charged_at) if chat.charged_at else None,
                total_coins_charged=0,
                coins_by_user={},
                reason="گفتگو قبلاً تسویه شده است.",
            )

        if not (chat.participant_one_sent_msg and chat.participant_two_sent_msg):
            return SettlementResult(
                chat_id=chat.id,
                status="NOT_ELIGIBLE",
                chat_type=chat.chat_type,
                charged_at=None,
                total_coins_charged=0,
                coins_by_user={},
                reason="تا زمانی که هر دو طرف حداقل یک پیام ارسال نکنند، هزینه‌ای کسر نمی‌شود.",
            )

        charges_by_user = await get_settlement_charges_for_chat_in_session(session, chat)
        ordered_payer_ids = sorted(charges_by_user.keys())

        # Deterministic ascending user_id lock order across wallets & daily allowances
        available_by_user: dict[int, int] = {}
        for uid in ordered_payer_ids:
            if auto_allocate_daily:
                wallet, allowance = await self._wallet_service.allocate_daily_allowance_in_session(
                    session, uid, utc_now=ts
                )
            else:
                wallet = await self._wallet_service.ensure_wallet_in_session(
                    session, uid, utc_now=ts, lock_for_update=True
                )
                allowance = await self._wallet_service._get_active_allowance_in_session(
                    session, uid, business_date, lock_for_update=True
                )
            bal = _build_balance_dto(wallet, allowance, ts)
            available_by_user[uid] = bal.total_spendable_coins

        insufficient_users = tuple(
            uid for uid in ordered_payer_ids if available_by_user[uid] < charges_by_user[uid]
        )
        if insufficient_users:
            # UNRESOLVED POLICY INTEGRATION POINT:
            # Product policy when a participant has insufficient balance at settlement time
            # (after both users have sent messages) is deferred for future product decision.
            # Preserve is_charged=False, charged_at=None, and zero settlement debits/debt.
            logger.warning(
                "Chat settlement deferred due to insufficient balance for chat=%s users=%s",
                chat.id,
                insufficient_users,
            )
            return SettlementResult(
                chat_id=chat.id,
                status="INSUFFICIENT_BALANCE",
                chat_type=chat.chat_type,
                charged_at=None,
                total_coins_charged=0,
                coins_by_user={},
                insufficient_user_ids=insufficient_users,
                required_coins_by_user=charges_by_user,
                available_coins_by_user=available_by_user,
                reason="موجودی سکه چت یک یا هر دو طرف برای تسویه گفتگو کافی نیست.",
            )

        ledger_ids: list[int] = []
        charged_map: dict[int, int] = {}
        for uid in ordered_payer_ids:
            cost = charges_by_user[uid]
            if cost <= 0:
                continue
            _, ledgers = await self._wallet_service.debit_coins_in_session(
                session,
                uid,
                cost,
                idempotency_key=f"chat_settle:{chat.id}:{uid}",
                transaction_type="CHAT_SETTLEMENT",
                actor_type="SYSTEM",
                actor_id=None,
                reference_id=str(chat.id),
                reason=f"Chat settlement ({chat.chat_type}) for session {chat.id}",
                allow_free=True,
                auto_allocate_daily=False,
                utc_now=ts,
            )
            charged_map[uid] = cost
            ledger_ids.extend(entry.id for entry in ledgers)

        chat.is_charged = True
        chat.charged_at = ts
        await session.flush()

        return SettlementResult(
            chat_id=chat.id,
            status="SETTLED",
            chat_type=chat.chat_type,
            charged_at=ts,
            total_coins_charged=sum(charged_map.values()),
            coins_by_user=charged_map,
            ledger_ids=tuple(ledger_ids),
            required_coins_by_user=charges_by_user,
            available_coins_by_user=available_by_user,
        )

    async def settle_chat(
        self,
        chat_id: uuid.UUID,
        *,
        auto_allocate_daily: bool = True,
        utc_now: datetime | None = None,
    ) -> SettlementResult:
        """Atomically settle chat_id in a single PostgreSQL transaction."""
        async with self._db_manager.session() as session:
            chat = (
                await session.execute(
                    select(ChatSession).where(ChatSession.id == chat_id).with_for_update()
                )
            ).scalar_one_or_none()
            if chat is None:
                raise LookupError("جلسه گفتگوی مورد نظر یافت نشد.")

            return await self.settle_chat_in_session(
                session,
                chat,
                auto_allocate_daily=auto_allocate_daily,
                utc_now=utc_now,
            )
