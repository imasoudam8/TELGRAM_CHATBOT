"""Domain service for durable wallets, daily free allowances, and immutable ledger entries."""

import hashlib
import logging
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import desc, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.config.settings import Settings, get_settings
from src.app.infrastructure.database.base import get_business_date, now_utc
from src.app.infrastructure.database.session import DatabaseManager
from src.app.modules.admin.models import SystemConfiguration
from src.app.modules.users.models import User
from src.app.modules.wallet.models import Wallet, WalletDailyAllowance, WalletLedger
from src.app.modules.wallet.schemas import (
    InsufficientBalanceError,
    InvalidAmountError,
    LedgerEntryDTO,
    TransactionHistoryPageDTO,
    WalletBalanceDTO,
    WalletPermissionError,
)

logger = logging.getLogger(__name__)

VALID_ACTOR_TYPES = frozenset({"SYSTEM", "USER", "ADMIN"})
SYSTEM_CONFIG_DAILY_QUOTA_KEY = "daily_free_coins_quota"


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def get_tehran_midnight_expiry(business_date: date, business_tz: ZoneInfo) -> datetime:
    """Compute the exact UTC timestamp for midnight ending business_date in business_tz.

    Safely handles daylight-saving transitions where 00:00:00 wall clock may skip or fold.
    """
    next_day = business_date + timedelta(days=1)
    midnight_next_day = datetime(
        next_day.year,
        next_day.month,
        next_day.day,
        0,
        0,
        0,
        fold=0,
        tzinfo=business_tz,
    )
    return midnight_next_day.astimezone(UTC)


def _subkey(base_key: str, suffix: str) -> str:
    candidate = f"{base_key}:{suffix}"
    if len(candidate) <= 128:
        return candidate
    digest = hashlib.sha256(base_key.encode("utf-8")).hexdigest()[:24]
    return f"k:{digest}:{suffix}"[:128]


def _ledger_to_dto(row: WalletLedger) -> LedgerEntryDTO:
    return LedgerEntryDTO(
        id=row.id,
        wallet_user_id=row.wallet_user_id,
        idempotency_key=row.idempotency_key,
        transaction_type=row.transaction_type,
        credit_type=row.credit_type,
        allowance_id=row.allowance_id,
        allowance_date=row.allowance_date,
        amount=row.amount,
        purchased_balance_before=row.purchased_balance_before,
        purchased_balance_after=row.purchased_balance_after,
        free_balance_before=row.free_balance_before,
        free_balance_after=row.free_balance_after,
        reference_id=row.reference_id,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        reason=row.reason,
        created_at=_as_utc(row.created_at),
    )


def _active_free_credits(allowance: WalletDailyAllowance | None, utc_now: datetime) -> int:
    if allowance is None:
        return 0
    if _as_utc(allowance.expires_at) <= utc_now:
        return 0
    return max(0, allowance.allocated_credits - allowance.used_credits)


def _build_balance_dto(
    wallet: Wallet,
    allowance: WalletDailyAllowance | None,
    utc_now: datetime,
) -> WalletBalanceDTO:
    free_coins = _active_free_credits(allowance, utc_now)
    purchased = max(0, wallet.purchased_coins)
    is_active = allowance is not None and _as_utc(allowance.expires_at) > utc_now
    return WalletBalanceDTO(
        user_id=wallet.user_id,
        purchased_coins=purchased,
        free_coins=free_coins,
        total_spendable_coins=purchased + free_coins,
        free_allowance_id=allowance.id if is_active and allowance is not None else None,
        free_allowance_date=allowance.allowance_date
        if is_active and allowance is not None
        else None,
        free_expires_at=_as_utc(allowance.expires_at)
        if is_active and allowance is not None
        else None,
    )


class WalletService:
    """Manages wallets, daily promotional credits, and append-only ledger transactions."""

    def __init__(self, db_manager: DatabaseManager, settings: Settings | None = None) -> None:
        self._db_manager = db_manager
        self._settings = settings or get_settings()

    @property
    def business_tz(self) -> ZoneInfo:
        """Return configured business timezone (Asia/Tehran)."""
        return self._settings.business_timezone

    async def get_effective_daily_quota(
        self,
        session: AsyncSession,
        *,
        override_quota: int | None = None,
    ) -> int:
        """Resolve daily free quota from explicit override, DB config, or Settings."""
        if override_quota is not None:
            if override_quota < 0:
                raise InvalidAmountError("سهمیه روزانه نمی‌تواند منفی باشد.")
            return override_quota

        cfg_val = (
            await session.execute(
                select(SystemConfiguration.value).where(
                    SystemConfiguration.key == SYSTEM_CONFIG_DAILY_QUOTA_KEY
                )
            )
        ).scalar_one_or_none()
        if isinstance(cfg_val, dict) and "quota" in cfg_val:
            try:
                parsed = int(cfg_val["quota"])
                if parsed >= 0:
                    return parsed
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid %s system configuration value", SYSTEM_CONFIG_DAILY_QUOTA_KEY
                )

        return max(0, int(self._settings.DAILY_FREE_COINS_QUOTA))

    async def _insert_ignore_wallet(
        self, session: AsyncSession, user_id: int, utc_now: datetime
    ) -> None:
        dialect_name = session.bind.dialect.name if session.bind is not None else "postgresql"
        values = {
            "user_id": user_id,
            "purchased_coins": 0,
            "created_at": utc_now,
            "updated_at": utc_now,
        }
        if dialect_name == "sqlite":
            stmt = (
                sqlite_insert(Wallet)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["user_id"])
            )
        else:
            stmt = (
                pg_insert(Wallet)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["user_id"])
            )
        await session.execute(stmt)

    async def ensure_wallet_in_session(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        utc_now: datetime | None = None,
        lock_for_update: bool = True,
    ) -> Wallet:
        """Ensure wallet exists for user_id and return it, optionally locked FOR UPDATE."""
        ts = _as_utc(utc_now) if utc_now is not None else now_utc()

        user_exists = (
            await session.execute(select(User.id).where(User.id == user_id))
        ).scalar_one_or_none()
        if user_exists is None:
            raise LookupError("کاربر مورد نظر یافت نشد.")

        await self._insert_ignore_wallet(session, user_id, ts)

        stmt = select(Wallet).where(Wallet.user_id == user_id)
        if lock_for_update:
            stmt = stmt.with_for_update()
        wallet = (await session.execute(stmt)).scalar_one()
        return wallet

    async def _get_active_allowance_in_session(
        self,
        session: AsyncSession,
        user_id: int,
        business_date: date,
        *,
        lock_for_update: bool = False,
    ) -> WalletDailyAllowance | None:
        stmt = select(WalletDailyAllowance).where(
            WalletDailyAllowance.user_id == user_id,
            WalletDailyAllowance.allowance_date == business_date,
        )
        if lock_for_update:
            stmt = stmt.with_for_update()
        return (await session.execute(stmt)).scalar_one_or_none()

    async def allocate_daily_allowance_in_session(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        quota_override: int | None = None,
        idempotency_key: str | None = None,
        utc_now: datetime | None = None,
    ) -> tuple[Wallet, WalletDailyAllowance]:
        """Atomically allocate today's Tehran-date free credits once within an active session."""
        ts = _as_utc(utc_now) if utc_now is not None else now_utc()
        business_date = get_business_date(ts, self.business_tz)
        expires_at = get_tehran_midnight_expiry(business_date, self.business_tz)

        wallet = await self.ensure_wallet_in_session(
            session, user_id, utc_now=ts, lock_for_update=True
        )

        existing = await self._get_active_allowance_in_session(
            session, user_id, business_date, lock_for_update=True
        )
        if existing is not None:
            return wallet, existing

        quota = await self.get_effective_daily_quota(session, override_quota=quota_override)

        dialect_name = session.bind.dialect.name if session.bind is not None else "postgresql"
        values = {
            "user_id": user_id,
            "allowance_date": business_date,
            "allocated_credits": quota,
            "used_credits": 0,
            "expires_at": expires_at,
            "created_at": ts,
            "updated_at": ts,
        }
        if dialect_name == "sqlite":
            insert_stmt = (
                sqlite_insert(WalletDailyAllowance)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["user_id", "allowance_date"])
                .returning(WalletDailyAllowance.id)
            )
        else:
            insert_stmt = (
                pg_insert(WalletDailyAllowance)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["user_id", "allowance_date"])
                .returning(WalletDailyAllowance.id)
            )

        inserted_id = (await session.execute(insert_stmt)).scalar_one_or_none()
        allowance = await self._get_active_allowance_in_session(
            session, user_id, business_date, lock_for_update=True
        )
        assert allowance is not None

        if inserted_id is not None and quota > 0:
            ledger_key = (
                idempotency_key.strip()[:128]
                if idempotency_key and idempotency_key.strip()
                else f"daily_grant:{user_id}:{business_date.isoformat()}"
            )
            already_logged = (
                await session.execute(
                    select(WalletLedger.id).where(WalletLedger.idempotency_key == ledger_key)
                )
            ).scalar_one_or_none()
            if already_logged is None:
                session.add(
                    WalletLedger(
                        wallet_user_id=user_id,
                        idempotency_key=ledger_key,
                        transaction_type="DAILY_GRANT",
                        credit_type="FREE",
                        allowance_id=allowance.id,
                        allowance_date=business_date,
                        amount=quota,
                        purchased_balance_before=wallet.purchased_coins,
                        purchased_balance_after=wallet.purchased_coins,
                        free_balance_before=0,
                        free_balance_after=quota,
                        reference_id=business_date.isoformat(),
                        actor_type="SYSTEM",
                        actor_id=None,
                        reason=f"Daily free Chat Coins quota for {business_date.isoformat()}",
                        created_at=ts,
                    )
                )
                await session.flush()

        return wallet, allowance

    async def ensure_wallet(
        self, user_id: int, *, utc_now: datetime | None = None
    ) -> WalletBalanceDTO:
        """Create wallet if absent and return current balances without auto-granting free coins."""
        ts = _as_utc(utc_now) if utc_now is not None else now_utc()
        business_date = get_business_date(ts, self.business_tz)
        async with self._db_manager.session() as session:
            wallet = await self.ensure_wallet_in_session(
                session, user_id, utc_now=ts, lock_for_update=False
            )
            allowance = await self._get_active_allowance_in_session(
                session, user_id, business_date, lock_for_update=False
            )
            return _build_balance_dto(wallet, allowance, ts)

    async def allocate_daily_allowance(
        self,
        user_id: int,
        *,
        quota_override: int | None = None,
        idempotency_key: str | None = None,
        utc_now: datetime | None = None,
    ) -> WalletBalanceDTO:
        """Idempotently allocate today's Tehran-date daily free allowance for user_id."""
        ts = _as_utc(utc_now) if utc_now is not None else now_utc()
        async with self._db_manager.session() as session:
            wallet, allowance = await self.allocate_daily_allowance_in_session(
                session,
                user_id,
                quota_override=quota_override,
                idempotency_key=idempotency_key,
                utc_now=ts,
            )
            return _build_balance_dto(wallet, allowance, ts)

    async def get_wallet_balance(
        self,
        user_id: int,
        *,
        auto_allocate_daily: bool = False,
        utc_now: datetime | None = None,
    ) -> WalletBalanceDTO:
        """Return purchased, daily free, and total spendable balance for user_id."""
        ts = _as_utc(utc_now) if utc_now is not None else now_utc()
        business_date = get_business_date(ts, self.business_tz)
        async with self._db_manager.session() as session:
            if auto_allocate_daily:
                wallet, allowance = await self.allocate_daily_allowance_in_session(
                    session, user_id, utc_now=ts
                )
            else:
                wallet = await self.ensure_wallet_in_session(
                    session, user_id, utc_now=ts, lock_for_update=False
                )
                allowance = await self._get_active_allowance_in_session(
                    session, user_id, business_date, lock_for_update=False
                )
            return _build_balance_dto(wallet, allowance, ts)

    async def credit_purchased_coins_in_session(
        self,
        session: AsyncSession,
        user_id: int,
        amount: int,
        *,
        idempotency_key: str,
        transaction_type: str = "PURCHASED_CREDIT",
        actor_type: str = "SYSTEM",
        actor_id: int | None = None,
        reference_id: str | None = None,
        reason: str,
        utc_now: datetime | None = None,
    ) -> tuple[WalletBalanceDTO, LedgerEntryDTO]:
        """Credit non-expiring purchased coins atomically and append an immutable ledger row."""
        if amount <= 0:
            raise InvalidAmountError("مبلغ تراکنش باید بزرگ‌تر از صفر باشد.")
        clean_key = idempotency_key.strip()
        if not clean_key:
            raise ValueError("کلید یکتای تراکنش (idempotency_key) الزامی است.")
        if len(clean_key) > 128:
            raise ValueError("طول کلید یکتا بیش از ۱۲۸ کاراکتر است.")
        clean_reason = reason.strip()
        if not clean_reason:
            raise ValueError("ذکر دلیل تراکنش الزامی است.")
        norm_actor_type = actor_type.upper()
        if norm_actor_type not in VALID_ACTOR_TYPES:
            raise ValueError("نوع عامل تراکنش نامعتبر است.")

        ts = _as_utc(utc_now) if utc_now is not None else now_utc()
        business_date = get_business_date(ts, self.business_tz)

        wallet = await self.ensure_wallet_in_session(
            session, user_id, utc_now=ts, lock_for_update=True
        )
        allowance = await self._get_active_allowance_in_session(
            session, user_id, business_date, lock_for_update=False
        )

        existing = (
            await session.execute(
                select(WalletLedger).where(WalletLedger.idempotency_key == clean_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.wallet_user_id != user_id:
                raise WalletPermissionError("کلید یکتای تراکنش متعلق به کاربر دیگری است.")
            return _build_balance_dto(wallet, allowance, ts), _ledger_to_dto(existing)

        current_free = _active_free_credits(allowance, ts)
        purchased_before = wallet.purchased_coins
        purchased_after = purchased_before + amount

        wallet.purchased_coins = purchased_after
        wallet.updated_at = ts

        ledger_row = WalletLedger(
            wallet_user_id=user_id,
            idempotency_key=clean_key,
            transaction_type=transaction_type,
            credit_type="PURCHASED",
            allowance_id=None,
            allowance_date=None,
            amount=amount,
            purchased_balance_before=purchased_before,
            purchased_balance_after=purchased_after,
            free_balance_before=current_free,
            free_balance_after=current_free,
            reference_id=reference_id[:64] if reference_id else None,
            actor_type=norm_actor_type,
            actor_id=actor_id,
            reason=clean_reason,
            created_at=ts,
        )
        session.add(ledger_row)
        await session.flush()
        await session.refresh(ledger_row)

        return _build_balance_dto(wallet, allowance, ts), _ledger_to_dto(ledger_row)

    async def credit_purchased_coins(
        self,
        user_id: int,
        amount: int,
        *,
        idempotency_key: str,
        transaction_type: str = "PURCHASED_CREDIT",
        actor_type: str = "SYSTEM",
        actor_id: int | None = None,
        reference_id: str | None = None,
        reason: str,
        utc_now: datetime | None = None,
    ) -> tuple[WalletBalanceDTO, LedgerEntryDTO]:
        """Idempotently credit non-expiring purchased coins in a PostgreSQL transaction."""
        async with self._db_manager.session() as session:
            return await self.credit_purchased_coins_in_session(
                session,
                user_id,
                amount,
                idempotency_key=idempotency_key,
                transaction_type=transaction_type,
                actor_type=actor_type,
                actor_id=actor_id,
                reference_id=reference_id,
                reason=reason,
                utc_now=utc_now,
            )

    async def debit_coins_in_session(
        self,
        session: AsyncSession,
        user_id: int,
        amount: int,
        *,
        idempotency_key: str,
        transaction_type: str = "PURCHASED_DEBIT",
        actor_type: str = "SYSTEM",
        actor_id: int | None = None,
        reference_id: str | None = None,
        reason: str,
        allow_free: bool = True,
        auto_allocate_daily: bool = False,
        utc_now: datetime | None = None,
    ) -> tuple[WalletBalanceDTO, tuple[LedgerEntryDTO, ...]]:
        """Atomically debit coins (daily FREE credits first, then PURCHASED) in one session."""
        if amount <= 0:
            raise InvalidAmountError("مبلغ کسر باید بزرگ‌تر از صفر باشد.")
        clean_key = idempotency_key.strip()
        if not clean_key:
            raise ValueError("کلید یکتای تراکنش (idempotency_key) الزامی است.")
        if len(clean_key) > 128:
            raise ValueError("طول کلید یکتا بیش از ۱۲۸ کاراکتر است.")
        clean_reason = reason.strip()
        if not clean_reason:
            raise ValueError("ذکر دلیل تراکنش الزامی است.")
        norm_actor_type = actor_type.upper()
        if norm_actor_type not in VALID_ACTOR_TYPES:
            raise ValueError("نوع عامل تراکنش نامعتبر است.")

        ts = _as_utc(utc_now) if utc_now is not None else now_utc()
        business_date = get_business_date(ts, self.business_tz)

        if auto_allocate_daily and allow_free:
            wallet, allowance = await self.allocate_daily_allowance_in_session(
                session, user_id, utc_now=ts
            )
        else:
            wallet = await self.ensure_wallet_in_session(
                session, user_id, utc_now=ts, lock_for_update=True
            )
            allowance = await self._get_active_allowance_in_session(
                session, user_id, business_date, lock_for_update=True
            )

        free_key = _subkey(clean_key, "free")
        purchased_key = _subkey(clean_key, "purchased")
        existing_rows = (
            (
                await session.execute(
                    select(WalletLedger)
                    .where(WalletLedger.idempotency_key.in_([clean_key, free_key, purchased_key]))
                    .order_by(WalletLedger.id)
                )
            )
            .scalars()
            .all()
        )
        if existing_rows:
            for row in existing_rows:
                if row.wallet_user_id != user_id:
                    raise WalletPermissionError("کلید یکتای تراکنش متعلق به کاربر دیگری است.")
            return _build_balance_dto(wallet, allowance, ts), tuple(
                _ledger_to_dto(r) for r in existing_rows
            )

        free_available = _active_free_credits(allowance, ts) if allow_free else 0
        purchased_available = max(0, wallet.purchased_coins)

        if free_available + purchased_available < amount:
            raise InsufficientBalanceError("موجودی سکه چت برای انجام این عملیات کافی نیست.")

        free_debit = min(free_available, amount)
        purchased_debit = amount - free_debit

        created_ledgers: list[WalletLedger] = []
        running_free = _active_free_credits(allowance, ts)
        running_purchased = purchased_available

        if free_debit > 0:
            assert allowance is not None
            key_for_free = free_key if purchased_debit > 0 else clean_key
            free_before = running_free
            free_after = running_free - free_debit
            allowance.used_credits += free_debit
            allowance.updated_at = ts

            free_entry = WalletLedger(
                wallet_user_id=user_id,
                idempotency_key=key_for_free,
                transaction_type=transaction_type,
                credit_type="FREE",
                allowance_id=allowance.id,
                allowance_date=allowance.allowance_date,
                amount=free_debit,
                purchased_balance_before=running_purchased,
                purchased_balance_after=running_purchased,
                free_balance_before=free_before,
                free_balance_after=free_after,
                reference_id=reference_id[:64] if reference_id else None,
                actor_type=norm_actor_type,
                actor_id=actor_id,
                reason=clean_reason,
                created_at=ts,
            )
            session.add(free_entry)
            created_ledgers.append(free_entry)
            running_free = free_after

        if purchased_debit > 0:
            key_for_purchased = purchased_key if free_debit > 0 else clean_key
            purchased_before = running_purchased
            purchased_after = running_purchased - purchased_debit
            wallet.purchased_coins = purchased_after
            wallet.updated_at = ts

            purchased_entry = WalletLedger(
                wallet_user_id=user_id,
                idempotency_key=key_for_purchased,
                transaction_type=transaction_type,
                credit_type="PURCHASED",
                allowance_id=None,
                allowance_date=None,
                amount=purchased_debit,
                purchased_balance_before=purchased_before,
                purchased_balance_after=purchased_after,
                free_balance_before=running_free,
                free_balance_after=running_free,
                reference_id=reference_id[:64] if reference_id else None,
                actor_type=norm_actor_type,
                actor_id=actor_id,
                reason=clean_reason,
                created_at=ts,
            )
            session.add(purchased_entry)
            created_ledgers.append(purchased_entry)

        await session.flush()
        for entry in created_ledgers:
            await session.refresh(entry)

        return _build_balance_dto(wallet, allowance, ts), tuple(
            _ledger_to_dto(entry) for entry in created_ledgers
        )

    async def debit_coins(
        self,
        user_id: int,
        amount: int,
        *,
        idempotency_key: str,
        transaction_type: str = "PURCHASED_DEBIT",
        actor_type: str = "SYSTEM",
        actor_id: int | None = None,
        reference_id: str | None = None,
        reason: str,
        allow_free: bool = True,
        auto_allocate_daily: bool = False,
        utc_now: datetime | None = None,
    ) -> tuple[WalletBalanceDTO, tuple[LedgerEntryDTO, ...]]:
        """Idempotently debit coins (daily free credits first, then purchased) in PostgreSQL."""
        async with self._db_manager.session() as session:
            return await self.debit_coins_in_session(
                session,
                user_id,
                amount,
                idempotency_key=idempotency_key,
                transaction_type=transaction_type,
                actor_type=actor_type,
                actor_id=actor_id,
                reference_id=reference_id,
                reason=reason,
                allow_free=allow_free,
                auto_allocate_daily=auto_allocate_daily,
                utc_now=utc_now,
            )

    async def admin_credit_coins(
        self,
        user_id: int,
        amount: int,
        *,
        admin_actor_id: int,
        reason: str,
        idempotency_key: str,
        reference_id: str | None = None,
        utc_now: datetime | None = None,
    ) -> tuple[WalletBalanceDTO, LedgerEntryDTO]:
        """Administrative/manual purchased coin grant with mandatory actor and reason."""
        if admin_actor_id <= 0:
            raise WalletPermissionError("شناسه اپراتور ادمین نامعتبر است.")
        if not reason.strip():
            raise ValueError("ذکر دلیل برای عملیات مالی ادمین الزامی است.")

        return await self.credit_purchased_coins(
            user_id=user_id,
            amount=amount,
            idempotency_key=idempotency_key,
            transaction_type="ADMIN_GRANT",
            actor_type="ADMIN",
            actor_id=admin_actor_id,
            reference_id=reference_id,
            reason=reason,
            utc_now=utc_now,
        )

    async def admin_debit_coins(
        self,
        user_id: int,
        amount: int,
        *,
        admin_actor_id: int,
        reason: str,
        idempotency_key: str,
        reference_id: str | None = None,
        allow_free: bool = False,
        utc_now: datetime | None = None,
    ) -> tuple[WalletBalanceDTO, tuple[LedgerEntryDTO, ...]]:
        """Administrative/manual coin debit with mandatory actor and reason."""
        if admin_actor_id <= 0:
            raise WalletPermissionError("شناسه اپراتور ادمین نامعتبر است.")
        if not reason.strip():
            raise ValueError("ذکر دلیل برای عملیات مالی ادمین الزامی است.")

        return await self.debit_coins(
            user_id=user_id,
            amount=amount,
            idempotency_key=idempotency_key,
            transaction_type="ADMIN_DEBIT",
            actor_type="ADMIN",
            actor_id=admin_actor_id,
            reference_id=reference_id,
            reason=reason,
            allow_free=allow_free,
            auto_allocate_daily=False,
            utc_now=utc_now,
        )

    async def get_transaction_history(
        self,
        user_id: int,
        *,
        offset: int = 0,
        limit: int = 10,
    ) -> TransactionHistoryPageDTO:
        """Return paginated immutable ledger entries for user_id ordered newest-first."""
        safe_offset = max(0, offset)
        safe_limit = max(1, min(limit, 50))

        async with self._db_manager.session() as session:
            total_count = int(
                (
                    await session.execute(
                        select(func.count(WalletLedger.id)).where(
                            WalletLedger.wallet_user_id == user_id
                        )
                    )
                ).scalar_one()
            )
            rows = (
                (
                    await session.execute(
                        select(WalletLedger)
                        .where(WalletLedger.wallet_user_id == user_id)
                        .order_by(desc(WalletLedger.created_at), desc(WalletLedger.id))
                        .offset(safe_offset)
                        .limit(safe_limit)
                    )
                )
                .scalars()
                .all()
            )
            return TransactionHistoryPageDTO(
                items=tuple(_ledger_to_dto(r) for r in rows),
                total_count=total_count,
                offset=safe_offset,
                limit=safe_limit,
                has_next=(safe_offset + len(rows)) < total_count,
                has_prev=safe_offset > 0,
            )
