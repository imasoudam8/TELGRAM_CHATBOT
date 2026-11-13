"""Allowlisted dynamic system configuration management and runtime config consumers."""

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.config.settings import Settings, get_settings
from src.app.infrastructure.database.base import now_utc
from src.app.infrastructure.database.session import DatabaseManager
from src.app.modules.admin.audit import record_audit_log_in_session
from src.app.modules.admin.models import SystemConfiguration
from src.app.modules.admin.rbac import SUPER_ADMIN_ONLY_ROLES, require_admin_actor_in_session
from src.app.modules.admin.schemas import (
    AdminNotFoundError,
    AdminValidationError,
    SystemConfigItemDTO,
)
from src.app.modules.moderation.enforcement import as_utc

logger = logging.getLogger(__name__)

KEY_DAILY_FREE_COINS_QUOTA = "daily_free_coins_quota"
KEY_RANDOM_CHAT_COST_COINS = "random_chat_cost_coins"
KEY_DIRECT_CHAT_COST_COINS = "direct_chat_cost_coins"
KEY_DIRECT_REQUEST_EXPIRY_HOURS = "direct_request_expiry_hours"
KEY_VIDEO_SIZE_LIMIT_BYTES = "video_size_limit_bytes"
KEY_MESSAGE_RETENTION_DAYS = "message_retention_days"

# Hard safety limits mandated by DB schema constraints
HARD_MAX_VIDEO_SIZE_BYTES = 10 * 1024 * 1024  # 10,485,760 bytes
HARD_MAX_MESSAGE_RETENTION_DAYS = 30
DEFAULT_RANDOM_CHAT_COST_COINS = 1
DEFAULT_DIRECT_CHAT_COST_COINS = 2
DEFAULT_DIRECT_REQUEST_EXPIRY_HOURS = 24


@dataclass(frozen=True)
class ConfigSpec:
    """Validation specification and metadata for an allowlisted config key."""

    key: str
    category: str
    field_name: str
    min_value: int
    max_value: int
    default_value: int
    label_persian: str
    description: str


ALLOWLISTED_SYSTEM_CONFIGS: dict[str, ConfigSpec] = {
    KEY_DAILY_FREE_COINS_QUOTA: ConfigSpec(
        key=KEY_DAILY_FREE_COINS_QUOTA,
        category="FINANCIAL",
        field_name="quota",
        min_value=0,
        max_value=1000,
        default_value=4,
        label_persian="سهمیه سکه رایگان روزانه",
        description="Daily free Chat Coins allocated per user per Tehran business date.",
    ),
    KEY_RANDOM_CHAT_COST_COINS: ConfigSpec(
        key=KEY_RANDOM_CHAT_COST_COINS,
        category="FINANCIAL",
        field_name="coins",
        min_value=0,
        max_value=100,
        default_value=DEFAULT_RANDOM_CHAT_COST_COINS,
        label_persian="هزینه گفتگوی تصادفی (سکه)",
        description="Chat Coins charged per participant on settled random chat sessions.",
    ),
    KEY_DIRECT_CHAT_COST_COINS: ConfigSpec(
        key=KEY_DIRECT_CHAT_COST_COINS,
        category="FINANCIAL",
        field_name="coins",
        min_value=0,
        max_value=100,
        default_value=DEFAULT_DIRECT_CHAT_COST_COINS,
        label_persian="هزینه درخواست مستقیم (سکه)",
        description="Chat Coins charged to requester on settled direct chat sessions.",
    ),
    KEY_DIRECT_REQUEST_EXPIRY_HOURS: ConfigSpec(
        key=KEY_DIRECT_REQUEST_EXPIRY_HOURS,
        category="CHAT",
        field_name="hours",
        min_value=1,
        max_value=168,
        default_value=DEFAULT_DIRECT_REQUEST_EXPIRY_HOURS,
        label_persian="مهلت انقضای درخواست مستقیم (ساعت)",
        description="Lifetime in hours before a PENDING direct chat request expires.",
    ),
    KEY_VIDEO_SIZE_LIMIT_BYTES: ConfigSpec(
        key=KEY_VIDEO_SIZE_LIMIT_BYTES,
        category="MEDIA",
        field_name="limit",
        min_value=1024,
        max_value=HARD_MAX_VIDEO_SIZE_BYTES,
        default_value=HARD_MAX_VIDEO_SIZE_BYTES,
        label_persian="حداکثر حجم ویدیو (بایت، سقف ۱۰ مگابایت)",
        description="Maximum allowed uploaded video file size in bytes (strict <= 10 MiB ceiling).",
    ),
    KEY_MESSAGE_RETENTION_DAYS: ConfigSpec(
        key=KEY_MESSAGE_RETENTION_DAYS,
        category="CHAT",
        field_name="days",
        min_value=1,
        max_value=HARD_MAX_MESSAGE_RETENTION_DAYS,
        default_value=HARD_MAX_MESSAGE_RETENTION_DAYS,
        label_persian="مدت نگهداری متادیتای پیام (روز، سقف ۳۰)",
        description="Message metadata retention period in days (strict <= 30 days ceiling).",
    ),
}


def validate_config_payload(
    key: str, raw_value: int | dict[str, Any]
) -> tuple[ConfigSpec, dict[str, int]]:
    """Strictly validate an allowlisted key, value type, JSON shape, and safety ceilings."""
    spec = ALLOWLISTED_SYSTEM_CONFIGS.get(key)
    if spec is None:
        raise AdminNotFoundError("کلید تنظیمات در فهرست مجاز سیستم قرار ندارد.")

    if isinstance(raw_value, dict):
        if set(raw_value.keys()) != {spec.field_name}:
            raise AdminValidationError(
                f"ساختار JSON برای {key} باید دقیقاً شامل کلید «{spec.field_name}» باشد."
            )
        candidate = raw_value[spec.field_name]
    else:
        candidate = raw_value

    if isinstance(candidate, bool) or not isinstance(candidate, int):
        raise AdminValidationError(f"مقدار «{spec.label_persian}» باید یک عدد صحیح معتبر باشد.")

    if candidate < spec.min_value or candidate > spec.max_value:
        raise AdminValidationError(
            f"مقدار «{spec.label_persian}» باید بین {spec.min_value} تا {spec.max_value} باشد."
        )

    return spec, {spec.field_name: candidate}


def _extract_effective_int(
    spec: ConfigSpec,
    persisted_value: dict[str, Any] | None,
    *,
    settings: Settings | None = None,
) -> int:
    fallback = spec.default_value
    if spec.key == KEY_DAILY_FREE_COINS_QUOTA and settings is not None:
        fallback = max(0, int(settings.DAILY_FREE_COINS_QUOTA))

    if isinstance(persisted_value, dict) and spec.field_name in persisted_value:
        val = persisted_value[spec.field_name]
        if not isinstance(val, bool) and isinstance(val, int):
            if spec.min_value <= val <= spec.max_value:
                return val
    return fallback


async def get_effective_config_int_in_session(
    session: AsyncSession,
    key: str,
    *,
    settings: Settings | None = None,
) -> int:
    """Resolve an allowlisted integer configuration value cleanly from PostgreSQL."""
    spec = ALLOWLISTED_SYSTEM_CONFIGS[key]
    raw_val = (
        await session.execute(
            select(SystemConfiguration.value).where(SystemConfiguration.key == key)
        )
    ).scalar_one_or_none()
    return _extract_effective_int(spec, raw_val, settings=settings)


async def get_effective_chat_tariffs_in_session(session: AsyncSession) -> tuple[int, int]:
    """Return (random_chat_cost_coins, direct_chat_cost_coins) honoring SystemConfiguration."""
    rows = (
        await session.execute(
            select(SystemConfiguration.key, SystemConfiguration.value).where(
                SystemConfiguration.key.in_(
                    [KEY_RANDOM_CHAT_COST_COINS, KEY_DIRECT_CHAT_COST_COINS]
                )
            )
        )
    ).all()
    by_key = dict(rows)
    random_cost = _extract_effective_int(
        ALLOWLISTED_SYSTEM_CONFIGS[KEY_RANDOM_CHAT_COST_COINS],
        by_key.get(KEY_RANDOM_CHAT_COST_COINS),
    )
    direct_cost = _extract_effective_int(
        ALLOWLISTED_SYSTEM_CONFIGS[KEY_DIRECT_CHAT_COST_COINS],
        by_key.get(KEY_DIRECT_CHAT_COST_COINS),
    )
    return random_cost, direct_cost


async def get_effective_video_size_limit_in_session(session: AsyncSession) -> int:
    """Return effective video limit in bytes, strictly bounded by 10 MiB hard ceiling."""
    val = await get_effective_config_int_in_session(session, KEY_VIDEO_SIZE_LIMIT_BYTES)
    return min(max(1024, val), HARD_MAX_VIDEO_SIZE_BYTES)


async def get_effective_message_retention_days_in_session(session: AsyncSession) -> int:
    """Return effective message retention in days, strictly bounded by 30-day ceiling."""
    val = await get_effective_config_int_in_session(session, KEY_MESSAGE_RETENTION_DAYS)
    return min(max(1, val), HARD_MAX_MESSAGE_RETENTION_DAYS)


async def get_effective_direct_request_expiry_hours_in_session(session: AsyncSession) -> int:
    """Return effective pending direct request expiry window in hours."""
    return await get_effective_config_int_in_session(session, KEY_DIRECT_REQUEST_EXPIRY_HOURS)


class AdminConfigService:
    """SUPER_ADMIN service for inspecting and updating allowlisted SystemConfiguration rows."""

    def __init__(self, db_manager: DatabaseManager, settings: Settings | None = None) -> None:
        self._db_manager = db_manager
        self._settings = settings or get_settings()

    async def list_configurations(self, actor_telegram_id: int) -> list[SystemConfigItemDTO]:
        """List all allowlisted configurations with persisted and effective runtime values."""
        async with self._db_manager.session() as session:
            await require_admin_actor_in_session(
                session,
                allowed_roles=SUPER_ADMIN_ONLY_ROLES,
                actor_telegram_id=actor_telegram_id,
            )
            rows = (
                (
                    await session.execute(
                        select(SystemConfiguration).where(
                            SystemConfiguration.key.in_(list(ALLOWLISTED_SYSTEM_CONFIGS.keys()))
                        )
                    )
                )
                .scalars()
                .all()
            )
            by_key = {row.key: row for row in rows}

            result: list[SystemConfigItemDTO] = []
            for key, spec in ALLOWLISTED_SYSTEM_CONFIGS.items():
                row = by_key.get(key)
                persisted = dict(row.value) if row is not None else None
                result.append(
                    SystemConfigItemDTO(
                        key=key,
                        category=spec.category,
                        field_name=spec.field_name,
                        persisted_value=persisted,
                        effective_int_value=_extract_effective_int(
                            spec, persisted, settings=self._settings
                        ),
                        min_value=spec.min_value,
                        max_value=spec.max_value,
                        label_persian=spec.label_persian,
                        description=row.description if row is not None else spec.description,
                        updated_at=as_utc(row.updated_at) if row is not None else None,
                    )
                )
            return result

    async def get_configuration(self, actor_telegram_id: int, key: str) -> SystemConfigItemDTO:
        """Retrieve a single allowlisted configuration entry for SUPER_ADMIN."""
        items = await self.list_configurations(actor_telegram_id)
        for item in items:
            if item.key == key:
                return item
        raise AdminNotFoundError("کلید تنظیمات مورد نظر یافت نشد.")

    async def update_configuration(
        self,
        actor_telegram_id: int,
        *,
        key: str,
        value: int | dict[str, Any],
        reason: str,
    ) -> tuple[SystemConfigItemDTO, int]:
        """Update an allowlisted configuration safely with row locking and audit logging."""
        spec, normalized_payload = validate_config_payload(key, value)
        clean_reason = reason.strip() if reason else ""
        if not clean_reason:
            raise AdminValidationError("ذکر دلیل برای تغییر تنظیمات سیستم الزامی است.")

        utc_now = now_utc()

        async with self._db_manager.session() as session:
            actor = await require_admin_actor_in_session(
                session,
                allowed_roles=SUPER_ADMIN_ONLY_ROLES,
                actor_telegram_id=actor_telegram_id,
            )

            existing = (
                await session.execute(
                    select(SystemConfiguration)
                    .where(SystemConfiguration.key == key)
                    .with_for_update()
                )
            ).scalar_one_or_none()

            payload_before = (
                dict(existing.value)
                if existing is not None
                else {
                    spec.field_name: _extract_effective_int(spec, None, settings=self._settings),
                    "source": "DEFAULT",
                }
            )

            if existing is None:
                row = SystemConfiguration(
                    key=key,
                    value=normalized_payload,
                    description=spec.description,
                    category=spec.category,
                    updated_by=actor.user_id,
                    updated_at=utc_now,
                )
                session.add(row)
            else:
                existing.value = normalized_payload
                existing.category = spec.category
                existing.updated_by = actor.user_id
                existing.updated_at = utc_now
                row = existing

            await session.flush()

            audit_entry = await record_audit_log_in_session(
                session,
                admin_user_id=actor.user_id,
                action="SYSTEM_CONFIG_UPDATE",
                target_type="SYSTEM_CONFIG",
                target_id=key,
                payload_before=payload_before,
                payload_after=normalized_payload,
                reason=clean_reason,
                utc_now=utc_now,
            )

            dto = SystemConfigItemDTO(
                key=key,
                category=spec.category,
                field_name=spec.field_name,
                persisted_value=dict(row.value),
                effective_int_value=normalized_payload[spec.field_name],
                min_value=spec.min_value,
                max_value=spec.max_value,
                label_persian=spec.label_persian,
                description=row.description,
                updated_at=as_utc(row.updated_at),
            )
            return dto, audit_entry.id
